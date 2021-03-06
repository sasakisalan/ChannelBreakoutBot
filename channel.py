﻿#_*_ coding: utf-8 _*_
#https://sshuhei.com

import pybitflyer
import json
import requests
import csv
import math
import pandas as pd
import time
import datetime
import logging
from pubnub.callbacks import SubscribeCallback
from pubnub.enums import PNStatusCategory
from pubnub.pnconfiguration import PNConfiguration
from pubnub.pubnub_tornado import PubNubTornado
from pubnub.pnconfiguration import PNReconnectionPolicy
from tornado import gen
import threading
from collections import deque

class ChannelBreakOut:
    def __init__(self):
        #config.jsonの読み込み
        f = open('config.json', 'r')
        config = json.load(f)
        #pubnubから取得した約定履歴を保存するリスト（基本的に不要．）
        self._executions = deque(maxlen=300)
        self._lot = 0.01
        self._product_code = config["product_code"]
        #各パラメタ．
        self._entryTerm = 10
        self._closeTerm = 5
        self._rangeTerm = 15
        self._rangeTh = 5000
        self._waitTerm = 5
        self._waitTh = 20000
        self._candleTerm = "1T"
        #現在のポジション．1ならロング．-1ならショート．0ならポジションなし．
        self._pos = 0
        #注文執行コスト．遅延などでこの値幅を最初から取られていると仮定する
        self._cost = 3000
        self.order = Order()
        #取引所のヘルスチェック
        self.healthCheck = config["healthCheck"]
        #ラインに稼働状況を通知
        self.line_notify_token = config["line_notify_token"]
        self.line_notify_api = 'https://notify-api.line.me/api/notify'

    @property
    def cost(self):
        return self._cost

    @cost.setter
    def cost(self, value):
        self._cost = value

    @property
    def candleTerm(self):
        return self._candleTerm
    @candleTerm.setter
    def candleTerm(self, val):
        """
        valは"5T"，"1H"などのString
        """
        self._candleTerm = val

    @property
    def waitTh(self):
        return self._waitTh
    @waitTh.setter
    def waitTh(self, val):
        self._waitTh = val

    @property
    def waitTerm(self):
        return self._waitTerm
    @waitTerm.setter
    def waitTerm(self, val):
        self._waitTerm = val

    @property
    def rangeTh(self):
        return self._rangeTh
    @rangeTh.setter
    def rangeTh(self,val):
        self._rangeTh = val

    @property
    def rangeTerm(self):
        return self._rangeTerm
    @rangeTerm.setter
    def rangeTerm(self,val):
        self._rangeTerm = val

    @property
    def executions(self):
        return self._executions
    @executions.setter
    def executions(self, val):
        self._executions = val

    @property
    def pos(self):
        return self._pos
    @pos.setter
    def pos(self, val):
        self._pos = int(val)

    @property
    def lot(self):
        return self._lot
    @lot.setter
    def lot(self, val):
        self._lot = round(val,3)

    @property
    def product_code(self):
        return self._product_code
    @product_code.setter
    def product_code(self, val):
        self._product_code = val

    @property
    def entryTerm(self):
        return self._entryTerm
    @entryTerm.setter
    def entryTerm(self, val):
        self._entryTerm = int(val)

    @property
    def closeTerm(self):
        return self._closeTerm
    @closeTerm.setter
    def closeTerm(self, val):
        self._closeTerm = int(val)

    def calculateLot(self, margin):
        """
        証拠金からロットを計算する関数．
        """
        lot = math.floor(margin*10**(-4))*10**(-2)
        return round(lot,2)

    def calculateLines(self, df_candleStick, term):
        """
        期間高値・安値を計算する．
        candleStickはcryptowatchのローソク足．termは安値，高値を計算する期間．（5ならローソク足5本文の安値，高値．)
        """
        lowLine = []
        highLine = []
        for i in range(len(df_candleStick.index)):
            if i < term:
                lowLine.append(df_candleStick["high"][i])
                highLine.append(df_candleStick["low"][i])
            else:
                low = min([price for price in df_candleStick["low"][i-term:i-1]])
                high = max([price for price in df_candleStick["high"][i-term:i-1]])
                lowLine.append(low)
                highLine.append(high)
        return (lowLine, highLine)

    def calculatePriceRange(self, df_candleStick, term):
        """
        termの期間の値幅を計算．
        """
        low = [min([df_candleStick["close"][i-term+1:i].min(),df_candleStick["open"][i-term+1:i].min()]) for i in range(len(df_candleStick.index))]
        high = [max([df_candleStick["close"][i-term+1:i].max(), df_candleStick["open"][i-term+1:i].max()]) for i in range(len(df_candleStick.index))]
        low = pd.Series(low)
        high = pd.Series(high)
        priceRange = [high.iloc[i]-low.iloc[i] for i in range(len(df_candleStick.index))]
        return priceRange

    def isRange(self, df_candleStick, term, th):
        """
        レンジ相場かどうかをTrue,Falseの配列で返す．termは期間高値・安値の計算期間．thはレンジ判定閾値．
        """
        #値幅での判定．
        if th != None:
            priceRange = self.calculatePriceRange(df_candleStick, term)
            isRange = [th > i for i in priceRange]
        #終値の標準偏差の差分が正か負かでの判定．
        elif th == None and term != None:
            df_candleStick["std"] = [df_candleStick["close"][i-term+1:i].std() for i in range(len(df_candleStick.index))]
            df_candleStick["std_slope"] = [df_candleStick["std"][i]-df_candleStick["std"][i-1] for i in range(len(df_candleStick.index))]
            isRange = [i > 0 for i in df_candleStick["std_slope"]]
        else:
            isRange = [False for i in df_candleStick.index]
        return isRange

    def judge(self, df_candleStick, entryHighLine, entryLowLine, closeHighLine, closeLowLine, entryTerm):
        """
        売り買い判断．ローソク足の高値が期間高値を上抜けたら買いエントリー．（2）ローソク足の安値が期間安値を下抜けたら売りエントリー．judgementリストは[買いエントリー，売りエントリー，買いクローズ（売り），売りクローズ（買い）]のリストになっている．（二次元リスト）リスト内リストはの要素は，0（シグナルなし）,価格（シグナル点灯）を取る．
        """
        judgement = [[0,0,0,0] for i in range(len(df_candleStick.index))]
        for i in range(len(df_candleStick.index)):
            #上抜けでエントリー
            if df_candleStick["high"][i] > entryHighLine[i] and i >= entryTerm:
                judgement[i][0] = entryHighLine[i]
            #下抜けでエントリー
            if df_candleStick["low"][i] < entryLowLine[i] and i >= entryTerm:
                judgement[i][1] = entryLowLine[i]
            #下抜けでクローズ
            if df_candleStick["low"][i] < closeLowLine[i] and i >= entryTerm:
                judgement[i][2] = closeLowLine[i]
            #上抜けでクローズ
            if df_candleStick["high"][i] > closeHighLine[i] and i >= entryTerm:
                judgement[i][3] = closeHighLine[i]
            #
            else:
                pass
        return judgement

    def judgeForLoop(self, high, low, entryHighLine, entryLowLine, closeHighLine, closeLowLine):
        """
        売り買い判断．入力した価格が期間高値より高ければ買いエントリー，期間安値を下抜けたら売りエントリー．judgementリストは[買いエントリー，売りエントリー，買いクローズ（売り），売りクローズ（買い）]のリストになっている．（値は0or1）
        ローソク足は1分ごとに取得するのでインデックスが-1のもの（現在より1本前）をつかう．
        """
        judgement = [0,0,0,0]
        #上抜けでエントリー
        if high > entryHighLine[-1]:
            judgement[0] = 1
        #下抜けでエントリー
        if low < entryLowLine[-1]:
            judgement[1] = 1
        #下抜けでクローズ
        if low < closeLowLine[-1]:
            judgement[2] = 1
        #上抜けでクローズ
        if high > closeHighLine[-1]:
            judgement[3] = 1
        return judgement

    #エントリーラインおよびクローズラインで約定すると仮定する．
    def backtest(self, judgement, df_candleStick, lot, rangeTh, rangeTerm, originalWaitTerm=10, waitTh=10000, cost = 0):
        #エントリーポイント，クローズポイントを入れるリスト
        buyEntrySignals = []
        sellEntrySignals = []
        buyCloseSignals = []
        sellCloseSignals = []
        nOfTrade = 0
        pos = 0
        pl = []
        pl.append(0)
        #トレードごとの損益
        plPerTrade = []
        #取引時の価格を入れる配列．この価格でバックテストのplを計算する．（ので，どの価格で約定するかはテストのパフォーマンスに大きく影響を与える．）
        buy_entry = []
        buy_close = []
        sell_entry = []
        sell_close = []
        #各ローソク足について，レンジ相場かどうかの判定が入っている配列
        isRange =  self.isRange(df_candleStick, rangeTerm, rangeTh)
        #基本ロット．勝ちトレードの直後はロットを落とす．
        originalLot = lot
        #勝ちトレード後，何回のトレードでロットを落とすか．
        waitTerm = 0
        for i in range(len(judgement)):
            if i > 0:
                lastPL = pl[-1]
                pl.append(lastPL)
            #エントリーロジック
            if pos == 0 and not isRange[i]:
                #ロングエントリー
                if judgement[i][0] != 0:
                    pos += 1
                    buy_entry.append(judgement[i][0])
                    buyEntrySignals.append(df_candleStick.index[i])
                #ショートエントリー
                elif judgement[i][1] != 0:
                    pos -= 1
                    sell_entry.append(judgement[i][1])
                    sellEntrySignals.append(df_candleStick.index[i])
            #ロングクローズロジック
            elif pos == 1:
                #ロングクローズ
                if judgement[i][2] != 0:
                    nOfTrade += 1
                    pos -= 1
                    buy_close.append(judgement[i][2])
                    #値幅
                    plRange = buy_close[-1] - buy_entry[-1]
                    pl[-1] = pl[-2] + (plRange-self.cost) * lot
                    buyCloseSignals.append(df_candleStick.index[i])
                    plPerTrade.append((plRange-self.cost)*lot)
                    #waitTh円以上の値幅を取った場合，次の10トレードはロットを1/10に落とす．
                    if plRange > waitTh:
                        waitTerm = originalWaitTerm
                        lot = originalLot/10
                    elif waitTerm > 0:
                        waitTerm -= 1
                        lot = originalLot/10
                    if waitTerm == 0:
                         lot = originalLot
            #ショートクローズロジック
            elif pos == -1:
                #ショートクローズ
                if judgement[i][3] != 0:
                    nOfTrade += 1
                    pos += 1
                    sell_close.append(judgement[i][3])
                    plRange = sell_entry[-1] - sell_close[-1]
                    pl[-1] = pl[-2] + (plRange-self.cost) * lot
                    sellCloseSignals.append(df_candleStick.index[i])
                    plPerTrade.append((plRange-self.cost)*lot)
                    #waitTh円以上の値幅を取った場合，次の10トレードはロットを1/10に落とす．
                    if plRange > waitTh:
                        waitTerm = originalWaitTerm
                        lot = originalLot/10
                    elif waitTerm > 0:
                        waitTerm -= 1
                        lot = originalLot/10
                    if waitTerm == 0:
                         lot = originalLot

            #さらに，クローズしたと同時にエントリーシグナルが出ていた場合のロジック．
            if pos == 0 and not isRange[i]:
                #ロングエントリー
                if judgement[i][0] != 0:
                    pos += 1
                    buy_entry.append(judgement[i][0])
                    buyEntrySignals.append(df_candleStick.index[i])
                #ショートエントリー
                elif judgement[i][1] != 0:
                    pos -= 1
                    sell_entry.append(judgement[i][1])
                    sellEntrySignals.append(df_candleStick.index[i])

        #最後にポジションを持っていたら，期間最後のローソク足の終値で反対売買．
        if pos == 1:
            buy_close.append(df_candleStick["close"][-1])
            plRange = buy_close[-1] - buy_entry[-1]
            pl[-1] = pl[-2] + plRange * lot
            pos -= 1
            buyCloseSignals.append(df_candleStick.index[-1])
            nOfTrade += 1
            plPerTrade.append(plRange*lot)
        elif pos ==-1:
            sell_close.append(df_candleStick["close"][-1])
            plRange = sell_entry[-1] - sell_close[-1]
            pl[-1] = pl[-2] + plRange * lot
            pos +=1
            sellCloseSignals.append(df_candleStick.index[-1])
            nOfTrade += 1
            plPerTrade.append(plRange*lot)
        return (pl, buyEntrySignals, sellEntrySignals, buyCloseSignals, sellCloseSignals, nOfTrade, plPerTrade)

    def describeResult(self, entryTerm, closeTerm, fileName=None, candleTerm=None, rangeTh=5000, rangeTerm=15, originalWaitTerm=10, waitTh=10000, showFigure=True, cost=0):
        """
        signalsは買い，売り，中立が入った配列
        """
        import matplotlib.pyplot as plt
        if fileName == None:
            if "H" in candleTerm:
                candleStick = self.getSpecifiedCandlestick(2000, "3600")
            else:
                candleStick = self.getSpecifiedCandlestick(5999, "60")
        else:
            candleStick = self.readDataFromFile(fileName)

        if candleTerm != None:
            df_candleStick = self.processCandleStick(candleStick, candleTerm)
        else:
            df_candleStick = self.fromListToDF(candleStick)

        entryLowLine, entryHighLine = self.calculateLines(df_candleStick, entryTerm)
        closeLowLine, closeHighLine = self.calculateLines(df_candleStick, closeTerm)
        judgement = self.judge(df_candleStick, entryHighLine, entryLowLine, closeHighLine, closeLowLine, entryTerm)
        pl, buyEntrySignals, sellEntrySignals, buyCloseSignals, sellCloseSignals, nOfTrade, plPerTrade = self.backtest(judgement, df_candleStick, 1, rangeTh, rangeTerm, originalWaitTerm=originalWaitTerm, waitTh=waitTh, cost=cost)

        if showFigure:
            plt.figure()
            plt.subplot(211)
            plt.plot(df_candleStick.index, df_candleStick["high"])
            plt.plot(df_candleStick.index, df_candleStick["low"])
            plt.ylabel("Price(JPY)")
            ymin = min(df_candleStick["low"]) - 200
            ymax = max(df_candleStick["high"]) + 200
            plt.vlines(buyEntrySignals, ymin , ymax, "blue", linestyles='dashed', linewidth=1)
            plt.vlines(sellEntrySignals, ymin , ymax, "red", linestyles='dashed', linewidth=1)
            plt.vlines(buyCloseSignals, ymin , ymax, "black", linestyles='dashed', linewidth=1)
            plt.vlines(sellCloseSignals, ymin , ymax, "green", linestyles='dashed', linewidth=1)
            plt.subplot(212)
            plt.plot(df_candleStick.index, pl)
            plt.hlines(y=0, xmin=df_candleStick.index[0], xmax=df_candleStick.index[-1], colors='k', linestyles='dashed')
            plt.ylabel("PL(JPY)")
        else:
            pass

        #各統計量の計算および表示．
        winTrade = sum([1 for i in plPerTrade if i > 0])
        loseTrade = sum([1 for i in plPerTrade if i < 0])
        winPer = round(winTrade/(winTrade+loseTrade) * 100,2)

        winTotal = sum([i for i in plPerTrade if i > 0])
        loseTotal = sum([i for i in plPerTrade if i < 0])
        profitFactor = round(winTotal/-loseTotal, 3)

        maxProfit = max(plPerTrade)
        maxLoss = min(plPerTrade)

        logging.info("Total pl: {}JPY".format(int(pl[-1])))
        logging.info("The number of Trades: {}".format(nOfTrade))
        logging.info("The Winning percentage: {}%".format(winPer))
        logging.info("The profitFactor: {}".format(profitFactor))
        logging.info("The maximum Profit and Loss: {}JPY, {}JPY".format(maxProfit, maxLoss))
        if showFigure:
            plt.show()
        else:
            plt.clf()
        return pl[-1], profitFactor

    def getCandlestick(self, number, period):
        """
        number:ローソク足の数．period:ローソク足の期間（文字列で秒数を指定，Ex:1分足なら"60"）．cryptowatchはときどきおかしなデータ（price=0）が含まれるのでそれを除く．
        """
        #ローソク足の時間を指定
        periods = [period]
        #クエリパラメータを指定
        query = {"periods":','.join(periods)}
        #ローソク足取得
        res = \
            json.loads(requests.get("https://api.cryptowat.ch/markets/bitflyer/btcfxjpy/ohlc", params=query).text)[
                "result"]
        # ローソク足のデータを入れる配列．
        data = []
        for i in periods:
            row = res[i]
            length = len(row)
            for column in row[:length - (number + 1):-1]:
                # dataへローソク足データを追加．
                if column[4] != 0:
                    column = column[0:6]
                    data.append(column)
        return data[::-1]


    def fromListToDF(self, candleStick):
        """
        Listのローソク足をpandasデータフレームへ．
        """
        date = [price[0] for price in candleStick]
        priceOpen = [int(price[1]) for price in candleStick]
        priceHigh = [int(price[2]) for price in candleStick]
        priceLow = [int(price[3]) for price in candleStick]
        priceClose = [int(price[4]) for price in candleStick]
        date_datetime = map(datetime.datetime.fromtimestamp, date)
        dti = pd.DatetimeIndex(date_datetime)
        df_candleStick = pd.DataFrame({"open" : priceOpen, "high" : priceHigh, "low": priceLow, "close" : priceClose}, index=dti)
        return df_candleStick

    def processCandleStick(self, candleStick, timeScale):
        """
        1分足データから各時間軸のデータを作成.timeScaleには5T（5分），H（1時間）などの文字列を入れる
        """
        df_candleStick = self.fromListToDF(candleStick)
        processed_candleStick = df_candleStick.resample(timeScale).agg({'open': 'first','high': 'max','low': 'min','close': 'last'})
        processed_candleStick = processed_candleStick.dropna()
        return processed_candleStick

    #csvファイル（ヘッダなし）からohlcデータを作成．
    def readDataFromFile(self, filename):
        for i in range(1, 10, 1):
            with open(filename, 'r', encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader)
                for row in reader:
                    candleStick = [row for row in reader if row[4] != "0"]
        dtDate = [datetime.datetime.strptime(data[0], '%Y-%m-%d %H:%M:%S') for data in candleStick]
        dtTimeStamp = [dt.timestamp() for dt in dtDate]
        for i in range(len(candleStick)):
            candleStick[i][0] = dtTimeStamp[i]
        candleStick = [[float(i) for i in data] for data in candleStick]
        return candleStick

    def lineNotify(self, message, fileName=None):
        payload = {'message': message}
        headers = {'Authorization': 'Bearer ' + self.line_notify_token}
        if fileName == None:
            try:
                requests.post(self.line_notify_api, data=payload, headers=headers)
            except:
                pass
        else:
            try:
                files = {"imageFile": open(fileName, "rb")}
                requests.post(self.line_notify_api, data=payload, headers=headers, files = files)
            except:
                pass

    def describePLForNotification(self, pl, df_candleStick):
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        close = df_candleStick["close"]
        index = range(len(pl))
        # figure
        fig = plt.figure(figsize=(20,12))
        #for price
        ax = fig.add_subplot(2, 1, 1)
        ax.plot(df_candleStick.index, close)
        ax.set_xlabel('Time')
        # y axis
        ax.set_ylabel('The price[JPY]')
        #for PLcurve
        ax = fig.add_subplot(2, 1, 2)
        # plot
        ax.plot(index, pl, color='b', label='The PL curve')
        ax.plot(index, [0]*len(pl), color='b',)
        # x axis
        ax.set_xlabel('The number of Trade')
        # y axis
        ax.set_ylabel('The estimated Profit/Loss(JPY)')
        # legend and title
        ax.legend(loc='best')
        ax.set_title('The PL curve(Time span:{})'.format(self.candleTerm))
        # save as png
        today = datetime.datetime.now().strftime('%Y%m%d')
        number = "_" + str(len(pl))
        fileName = today + number + ".png"
        plt.savefig(fileName)
        plt.close()

        return fileName

    def loop(self, entryTerm, closeTerm, rangeTh, rangeTerm, originalWaitTerm, waitTh, candleTerm=None):
        """
        注文の実行ループを回す関数
        """
        self.executionsProcess()
        #pubnubが回り始めるまで待つ．
        time.sleep(20)
        pos = 0
        pl = []
        pl.append(0)
        lastPositionPrice = 0
        lot = self.lot
        originalLot = self.lot
        waitTerm = 0

        try:
            if "H" in candleTerm:
                candleStick = self.getCandlestick(480, "3600")
            else:
                candleStick = self.getCandlestick(480, "60")
        except:
            logging.error("Unknown error happend when you requested candleStick")

        if candleTerm == None:
            df_candleStick = self.fromListToDF(candleStick)
        else:
            df_candleStick = self.processCandleStick(candleStick, candleTerm)

        entryLowLine, entryHighLine = self.calculateLines(df_candleStick, entryTerm)
        closeLowLine, closeHighLine = self.calculateLines(df_candleStick, closeTerm)

        #直近約定件数30件の高値と安値
        high = max([self.executions[-1-i]["price"] for i in range(30)])
        low = min([self.executions[-1-i]["price"] for i in range(30)])

        message = "Starting for channelbreak."
        logging.info(message)
        self.lineNotify(message)

        exeTimer1 = 0
        exeTimer5 = 0
        while True:
            logging.info('================================')
            exeMin = datetime.datetime.now().minute
            #1分ごとに基準ラインを更新
            if exeMin + 1 > exeTimer1 or (exeMin == 0 and exeTimer1 == 60):
                exeTimer1 = exeMin + 1
                logging.info("Renewing candleSticks")
                try:
                    if "H" in candleTerm:
                        candleStick = self.getCandlestick(480, "3600")
                    else:
                        candleStick = self.getCandlestick(480, "60")
                except:
                    logging.error("Unknown error happend when you requested candleStick")

                if candleTerm == None:
                    df_candleStick = self.fromListToDF(candleStick)
                else:
                    df_candleStick = self.processCandleStick(candleStick, candleTerm)

                entryLowLine, entryHighLine = self.calculateLines(df_candleStick, entryTerm)
                closeLowLine, closeHighLine = self.calculateLines(df_candleStick, closeTerm)
            else:
                pass

            #直近約定件数30件の高値と安値
            high = max([self.executions[-1-i]["price"] for i in range(30)])
            low = min([self.executions[-1-i]["price"] for i in range(30)])
            #売り買い判定
            judgement = self.judgeForLoop(high, low, entryHighLine, entryLowLine, closeHighLine, closeLowLine)
            #現在レンジ相場かどうか．
            isRange = self.isRange(df_candleStick, rangeTerm, rangeTh)

            #取引所のヘルスチェック
            boardState = self.order.getboardstate()
            serverHealth = True
            if (boardState["health"] == "NORMAL" or boardState["health"] == "BUSY" or boardState["health"] == "VERY BUSY") and boardState["state"] == "RUNNING" and self.healthCheck:
                pass
            elif self.healthCheck:
                serverHealth = False
                logging.info('Server is %s. Do not order.', boardState["health"],)

            #ログ出力
            logging.info('high:%s low:%s isRange:%s', high, low, isRange[-1])
            logging.info('entryHighLine:%s entryLowLine:%s closeHighLine:%s closeLowLine:%s', entryHighLine[-1], entryLowLine[-1], closeHighLine[-1], closeLowLine[-1])
            logging.info('Server Health is:%s State is:%s', boardState["health"], boardState["state"])

            #ここからエントリー，クローズ処理
            if pos == 0 and not isRange[-1] and serverHealth:
                #ロングエントリー
                if judgement[0]:
                    logging.info("Long entry order")
                    orderId = self.order.market(size=lot, side="BUY")
                    pos += 1
                    childOrder = self.order.getexecutions(orderId["child_order_acceptance_id"])
                    best_ask = childOrder[0]["price"]
                    message = "Long entry. Lot:{}, Price:{}".format(lot, best_ask)
                    self.lineNotify(message)
                    logging.info(message)
                    lastPositionPrice = best_ask
                #ショートエントリー
                elif judgement[1]:
                    logging.info("Short entry order")
                    orderId = self.order.market(size=lot,side="SELL")
                    pos -= 1
                    childOrder = self.order.getexecutions(orderId["child_order_acceptance_id"])
                    best_bid = childOrder[0]["price"]
                    message = "Short entry. Lot:{}, Price:{}, ".format(lot, best_bid)
                    self.lineNotify(message)
                    logging.info(message)
                    lastPositionPrice = best_bid

            elif pos == 1:
                #ロングクローズ
                if judgement[2]:
                    logging.info("Long close order")
                    orderId = self.order.market(size=lot,side="SELL")
                    pos -= 1
                    childOrder = self.order.getexecutions(orderId["child_order_acceptance_id"])
                    best_bid = childOrder[0]["price"]
                    plRange = best_bid - lastPositionPrice
                    pl.append(pl[-1] + plRange * lot)

                    message = "Long close. Lot:{}, Price:{}, pl:{}".format(lot, best_bid, pl[-1])
                    fileName = self.describePLForNotification(pl, df_candleStick)
                    self.lineNotify(message,fileName)
                    logging.info(message)

                    #一定以上の値幅を取った場合，次の10トレードはロットを1/10に落とす．
                    if plRange > waitTh:
                        waitTerm = originalWaitTerm
                        lot = round(originalLot/10,3)
                    if waitTerm > 0:
                        waitTerm -= 1
                        lot = round(originalLot/10,3)
                    if waitTerm == 0:
                         lot = originalLot

            elif pos == -1:
                #ショートクローズ
                if judgement[3]:
                    logging.info("Short close order")
                    orderId = self.order.market(size=lot, side="BUY")
                    pos += 1
                    childOrder = self.order.getexecutions(orderId["child_order_acceptance_id"])
                    best_ask = childOrder[0]["price"]
                    plRange = lastPositionPrice - best_ask
                    pl.append(pl[-1] + plRange * lot)

                    message = "Short close. Lot:{}, Price:{}, pl:{}".format(lot, best_ask, pl[-1])
                    fileName = self.describePLForNotification(pl, df_candleStick)
                    self.lineNotify(message,fileName)
                    logging.info(message)
                    #一定以上の値幅を取った場合，次の10トレードはロットを1/10に落とす．
                    if plRange > waitTh:
                        waitTerm = originalWaitTerm
                        lot = round(originalLot/10,3)
                    if waitTerm > 0:
                        waitTerm -= 1
                        lot = round(originalLot/10,3)
                    if waitTerm == 0:
                         lot = originalLot
            
            #クローズしたと同時にエントリーシグナルが出ていた場合にドテン売買
            if pos == 0 and not isRange[-1] and serverHealth:
                #ロングエントリー
                if judgement[0]:
                    logging.info("Long doten entry order")
                    orderId = self.order.market(size=lot, side="BUY")
                    pos += 1
                    childOrder = self.order.getexecutions(orderId["child_order_acceptance_id"])
                    best_ask = childOrder[0]["price"]
                    message = "Long entry. Lot:{}, Price:{}".format(lot, best_ask)
                    self.lineNotify(message)
                    logging.info(message)
                    lastPositionPrice = best_ask
                #ショートエントリー
                elif judgement[1]:
                    logging.info("Short doten entry order")
                    orderId = self.order.market(size=lot,side="SELL")
                    pos -= 1
                    childOrder = self.order.getexecutions(orderId["child_order_acceptance_id"])
                    best_bid = childOrder[0]["price"]
                    message = "Short entry. Lot:{}, Price:{}, ".format(lot, best_bid)
                    self.lineNotify(message)
                    logging.info(message)
                    lastPositionPrice = best_bid

            if (exeMin + 1 > exeTimer5 or (exeMin == 0 and exeTimer5 == 60)) and exeMin % 5 == 0:
                exeTimer5 = exeMin + 5
                message = "Waiting for channelbreaking."
                logging.info(message)

            time.sleep(2)

    def executionsProcess(self):
        """
        pubnubで価格を取得する場合の処理（基本的に不要．）
        """
        channels = ["lightning_executions_FX_BTC_JPY"]
        executions = self.executions
        class BFSubscriberCallback(SubscribeCallback):
            def message(self, pubnub, message):
                execution = message.message
                for i in execution:
                    executions.append(i)

        config = PNConfiguration()
        config.subscribe_key = 'sub-c-52a9ab50-291b-11e5-baaa-0619f8945a4f'
        config.reconnect_policy = PNReconnectionPolicy.LINEAR
        config.ssl = False
        config.set_presence_timeout(60)
        pubnub = PubNubTornado(config)
        listener = BFSubscriberCallback()
        pubnub.add_listener(listener)
        pubnub.subscribe().channels(channels).execute()
        pubnubThread = threading.Thread(target=pubnub.start)
        pubnubThread.start()

    def getSpecifiedCandlestick(self, number, period):
        """
        number:ローソク足の数．period:ローソク足の期間（文字列で秒数を指定，Ex:1分足なら"60"）．cryptowatchはときどきおかしなデータ（price=0）が含まれるのでそれを除く
        """
        # ローソク足の時間を指定
        periods = [period]
        # クエリパラメータを指定
        query = {"periods": ','.join(periods), "after": 1}
        # ローソク足取得
        try:
            res = json.loads(requests.get("https://api.cryptowat.ch/markets/bitflyer/btcfxjpy/ohlc", params=query).text)
            res = res["result"]
        except:
            logging.error(res)
        # ローソク足のデータを入れる配列．
        data = []
        for i in periods:
            row = res[i]
            length = len(row)
            for column in row[:length - (number + 1):-1]:
                # dataへローソク足データを追加．
                if column[4] != 0:
                    column = column[0:6]
                    data.append(column)
        return data[::-1]
    
    def test(self):
        pass

#注文処理をまとめている
class Order:
    def __init__(self):
        #config.jsonの読み込み
        f = open('config.json', 'r')
        config = json.load(f)
        self.product_code = config["product_code"]
        self.key = config["key"]
        self.secret = config["secret"]
        self.api = pybitflyer.API(self.key, self.secret)

    def limit(self, side, price, size, minute_to_expire=None):
        logging.info("Order: Limit. Side : {}".format(side))
        response = {"status":"internalError in order.py"}
        try:
            response = self.api.sendchildorder(product_code=self.product_code, child_order_type="LIMIT", side=side, price=price, size=size, minute_to_expire = minute_to_expire)
        except:
            pass
        logging.debug(response)
        while "status" in response:
            try:
                response = self.api.sendchildorder(product_code=self.product_code, child_order_type="LIMIT", side=side, price=price, size=size, minute_to_expire = minute_to_expire)
            except:
                pass
            logging.debug(response)
            time.sleep(3)
        return response

    def market(self, side, size, minute_to_expire= None):
        logging.info("Order: Market. Side : {}".format(side))
        response = {"status": "internalError in order.py"}
        try:
            response = self.api.sendchildorder(product_code=self.product_code, child_order_type="MARKET", side=side, size=size, minute_to_expire = minute_to_expire)
        except:
            pass
        logging.debug(response)
        while "status" in response:
            try:
                response = self.api.sendchildorder(product_code=self.product_code, child_order_type="MARKET", side=side, size=size, minute_to_expire = minute_to_expire)
            except:
                pass
            logging.debug(response)
            time.sleep(3)
        return response

    def ticker(self):
        response = {"status": "internalError in order.py"}
        try:
            response = self.api.ticker(product_code=self.product_code)
        except:
            pass
        logging.debug(response)
        while "status" in response:
            try:
                response = self.api.ticker(product_code=self.product_code)
            except:
                pass
            logging.debug(response)
        return response

    def getexecutions(self, order_id):
        response = {"status": "internalError in order.py"}
        try:
            response = self.api.getexecutions(product_code=self.product_code, child_order_acceptance_id=order_id)
        except:
            pass
        logging.debug(response)
        while ("status" in response or not response):
            try:
                response = self.api.getexecutions(product_code=self.product_code, child_order_acceptance_id=order_id)
            except:
                pass
            logging.debug(response)
            time.sleep(0.5)
        return response

    def getboardstate(self):
        response = {"status": "internalError in order.py"}
        try:
            response = self.api.getboardstate(product_code=self.product_code)
        except:
            pass
        logging.debug(response)
        while "status" in response:
            try:
                response = self.api.getboardstate(product_code=self.product_code)
            except:
                pass
            logging.debug(response)
            time.sleep(0.5)
        return response

    def stop(self, side, size, trigger_price, minute_to_expire=None):
        logging.info("Order: Stop. Side : {}".format(side))
        response = {"status": "internalError in order.py"}
        try:
            response = self.api.sendparentorder(order_method="SIMPLE", parameters=[{"product_code": self.product_code, "condition_type": "STOP", "side": side, "size": size,"trigger_price": trigger_price, "minute_to_expire": minute_to_expire}])
        except:
            pass
        logging.debug(response)
        while "status" in response:
            try:
                response = self.api.sendparentorder(order_method="SIMPLE", parameters=[{"product_code": self.product_code, "condition_type": "STOP", "side": side, "size": size,"trigger_price": trigger_price, "minute_to_expire": minute_to_expire}])
            except:
                pass
            logging.debug(response)
            time.sleep(3)
        return response

    def stop_limit(self, side, size, trigger_price, price, minute_to_expire=None):
        logging.info("Side : {}".format(side))
        response = {"status": "internalError in order.py"}
        try:
            response = self.api.sendparentorder(order_method="SIMPLE", parameters=[{"product_code": self.product_code, "condition_type": "STOP_LIMIT", "side": side, "size": size,"trigger_price": trigger_price, "price": price, "minute_to_expire": minute_to_expire}])
        except:
            pass
        logging.debug(response)
        while "status" in response:
            try:
                response = self.api.sendparentorder(order_method="SIMPLE", parameters=[{"product_code": self.product_code, "condition_type": "STOP_LIMIT", "side": side, "size": size,"trigger_price": trigger_price, "price": price, "minute_to_expire": minute_to_expire}])
            except:
                pass
            logging.debug(response)
        return response

    def trailing(self, side, size, offset, minute_to_expire=None):
        logging.info("Side : {}".format(side))
        response = {"status": "internalError in order.py"}
        try:
            response = self.api.sendparentorder(order_method="SIMPLE", parameters=[{"product_code": self.product_code, "condition_type": "TRAIL", "side": side, "size": size, "offset": offset, "minute_to_expire": minute_to_expire}])
        except:
            pass
        logging.debug(response)
        while "status" in response:
            try:
                response = self.api.sendparentorder(order_method="SIMPLE", parameters=[{"product_code": self.product_code, "condition_type": "TRAIL", "side": side, "size": size, "offset": offset, "minute_to_expire": minute_to_expire}])
            except:
                pass
            logging.debug(response)
        return response


def optimization(candleTerm):
    entryAndCloseTerm = [(2,2),(3,2),(2,3),(3,3),(4,2),(2,4),(4,3),(3,4),(4,4),(5,2),(2,5),(5,3),(3,5),(5,4),(4,5),(5,5),(10,10)]
    rangeThAndrangeTerm = [(None,3),(5000,3),(10000,3),(None,5),(5000,5),(10000,5),(None,10),(5000,10),(10000,10),(None,15),(5000,15),(10000,15),(None,None)]
    waitTermAndwaitTh = [(0,0),(3,10000),(3,15000),(3,20000),(5,10000),(5,15000),(5,20000),(10,10000),(10,15000),(10,20000),(15,10000),(15,15000),(15,20000)]

    paramList = []
    for i in entryAndCloseTerm:
        for j in rangeThAndrangeTerm:
            for k in waitTermAndwaitTh:
                channelBreakOut = ChannelBreakOut()
                channelBreakOut.entryTerm = i[0]
                channelBreakOut.closeTerm = i[1]
                channelBreakOut.rangeTh = j[0]
                channelBreakOut.rangeTerm = j[1]
                channelBreakOut.waitTerm = k[0]
                channelBreakOut.waitTh = k[1]
                channelBreakOut.candleTerm = candleTerm
                logging.info('================================')
                logging.info('entryTerm:%s closeTerm:%s rangeTerm:%s rangeTh:%s waitTerm:%s waitTh:%s candleTerm:%s',i[0],i[1],j[1],j[0],k[0],k[1],channelBreakOut.candleTerm)
                #テスト
                pl, profitFactor =  channelBreakOut.describeResult(entryTerm=channelBreakOut.entryTerm, closeTerm=channelBreakOut.closeTerm, rangeTh=channelBreakOut.rangeTh, rangeTerm=channelBreakOut.rangeTerm, originalWaitTerm=channelBreakOut.waitTerm, waitTh=channelBreakOut.waitTh, candleTerm=channelBreakOut.candleTerm, fileName="chart.csv", showFigure=False)
                paramList.append([pl,profitFactor, i,j,k])
    
    pF = [i[1] for i in paramList]
    pL = [i[0] for i in paramList]
    logging.info("======Search finished======")
    logging.info('Search pattern :%s', len(paramList))
    logging.info("Parameters:")
    logging.info("(entryTerm, closeTerm), (rangeTh, rangeTerm), (waitTerm, waitTh)")
    logging.info("ProfitFactor max:")
    logging.info(paramList[pF.index(max(pF))])
    logging.info("PL max:")
    logging.info(paramList[pL.index(max(pL))])

if __name__ == '__main__':
    #logging設定
    logging.basicConfig(
        filename='channelBreakOut.log',
        level=logging.INFO,
        format='%(asctime)s %(levelname)s: %(message)s',
        datefmt='%m/%d/%Y %I:%M:%S %p')
    console=logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        fmt='%(asctime)s %(levelname)s: %(message)s',
        datefmt='%m/%d/%Y %I:%M:%S %p'))
    logging.getLogger('').addHandler(console)
    logging.info('Wait...')
    
    #config.jsonの読み込み
    f = open('config.json', 'r')
    config = json.load(f)

    #channelBreakOut設定値
    channelBreakOut = ChannelBreakOut()
    channelBreakOut.entryTerm = config["entryTerm"]
    channelBreakOut.closeTerm = config["closeTerm"]
    channelBreakOut.rangeTerm = config["rangeTerm"]
    channelBreakOut.rangeTh = config["rangeTh"]
    channelBreakOut.waitTerm = config["waitTerm"]
    channelBreakOut.waitTh = config["waitTh"]
    channelBreakOut.candleTerm = config["candleTerm"]
    channelBreakOut.cost = config["cost"]

    if config["trading"]:
        #実働
        channelBreakOut.loop(channelBreakOut.entryTerm, channelBreakOut.closeTerm, channelBreakOut.rangeTh, channelBreakOut.rangeTerm, channelBreakOut.waitTerm, channelBreakOut.waitTh, channelBreakOut.candleTerm)
    elif config["backtest"]:
        #バックテスト
        channelBreakOut.describeResult(entryTerm=channelBreakOut.entryTerm, closeTerm=channelBreakOut.closeTerm, rangeTh=channelBreakOut.rangeTh, rangeTerm=channelBreakOut.rangeTerm, originalWaitTerm=channelBreakOut.waitTerm, waitTh=channelBreakOut.waitTh, candleTerm=channelBreakOut.candleTerm, showFigure=True, cost=channelBreakOut.cost)
    elif config["optimization"]:
        #最適化
        optimization(candleTerm=channelBreakOut.candleTerm)
    else:
        channelBreakOut.test()
