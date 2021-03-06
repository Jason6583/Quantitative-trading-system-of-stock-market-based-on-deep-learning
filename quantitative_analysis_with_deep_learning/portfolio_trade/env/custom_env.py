"""
    Portfolio_Prediction_Env

    依据观察和预测进行决策的资产组合交易环境。包括PortfolioManager和StockManager
    根据论文中的设定，一个step包括两个变化，持有期内股价变化，交易期内资产向量变化，交易时产生交易摩擦和交易阻碍


    参数：
        data source：
            股票池行情Q_t                        shape:(股票池宽度N, 历史时间T, 行情列数quote_col_num)
            预测股价的step by step历史Prd_t       shape:(股票池宽度N, 预测历史时间T', 预测长度pred_len)
            预测均方误差（转化为风险系数）Var_t    shape:(股票池宽度N, 预测历史时间T', 1)
            资产价格P_t                          shape:(历史时间T, n_asset+1, )
            持有量历史V_t（用于计算总资产）        shape:(历史时间T, n_asset+1, )
            资产分配比例W_t（用于计算action）      shape:(历史时间T, n_asset+1, )

        action space：
                Box([(0,1),(-10,10)], shape=(2, n_asset+1)),      
                # action space只支持Box类型数据，将资产分配比例和价格浮动比例合并
                # 资产分配比例，用于计算交易方向和交易量
                # 交易价格相比当日价格浮动比例，用于计算交易价格,其中现金比例固定为1


        observation space：
            # 对应于data source的全部数据，不再需要窗口数据，只需要股票池对应的数据，
            # Gym.Env只支持Box类型的obs space，但是GoalEnv可以支持Dict类型的obs
            # 使用GoalEnv，需要将obs设置为行情与预测的观察，achieved设置为资产向量，desired设置为奖励
            # 宽度与action space一致， n_asset+1
            （实际上，只需要对n_asset预测即可，通过配置确定从池中选择哪些股票进行投资）
                Dict{
                    observation：Box(low=-100, high=100, shape=(n_asset, 行情列数quote_col_num + 预测长度pred_len + 准确率和损失acc+loss)),
                    achieved_goal:Box(shape=(3, n_asset + 1)),  # P,V,W
                    desired_goal:Box(shape=(2,))  # reward
                }

        constraint condition:
            1.总资产 = 资产持有量 * 资产价格        A_t = V_t * P_t
            2.sum(资产分配比例) == 1               sum(W_t_i) == 1
            3.交易向量 = 资产分配比例差值           T_t = Weight_t+1 - W_t
            4.挂单价格 = 当前资产价格乘以波动率      Order_price = P_t * Percentage [1:]
        
        order generation:
            1.处理Agent得到的交易向量和挂单价格向量。
            2.交易金额 = 总资产 * 交易向量          Order_amount = A_t * T_t
            3.对交易金额每个分量训练判断：
                round：四舍五入
                买入 buy_t+1_i ：round(Order_amount_i / Order_price_i)/100 > 0
                卖出 sell_t+1_i ：round(Order_amount_i / Order_price_i)/100 < 0
                持有 ：round(Order_amount_i / Order_price_i)/100 == 0
            4.订单历史记录本地

        trade process：用于模拟回测
            1.处理订单，默认在每个新交易日开始挂单
                买入订单：Order_price_i > low_price 判断成交
                卖出订单：Order_price_i < high_price 判断成交
            2.交易损耗（手续费）：成交额损耗 u = 0.001，不分买入卖出
                买入，资金减少量 = buy_t+1_i * Order_price_i * (1 + u)
                卖出，资金增加量 = sell_t+1_i * Order_price_i * (1 - u)
            3.订单完成之后，改变持有量，持有量每个分量都是100的倍数（除了现金），默认每个订单都完整成交

        step:
            obs, reward, done, info
            reward：论文中使用log比例，也有分为浮动收益和固定收益，我们总体上希望 log(A_t+1/A_0) 最大
                    或者在一个episode内，最终的A_t最大。
            done:达到steps总数，则一个episode结束，另外如果亏损过大或者最大回撤过大，超过阈值也可以早死亡。

    方法：


    By LongFly

"""
import os
import sys
import json
import gym
from gym.spaces import Box, Dict
import pandas as pd 
import numpy as np 
import arrow
import random
from sklearn.metrics import mean_squared_error as MSE

from utils.data_process import DataProcessor 
from vnpy.trader.constant import Status, Direction

# 买卖
BUY = Direction.LONG
SELL = Direction.SHORT
# 订单状态:提交、交易成功、取消（人为取消）、拒单(交易不成功则拒单)
SUBMIT = Status.SUBMITTING
TRADE = Status.ALLTRADED
CANCEL = Status.CANCELLED
REJECT = Status.REJECTED

class Order(object):
    """
    订单类
    """
    def __init__(self, stock_symbol, direction, price, volume, status):
        """
        
        """
        self.orderid = arrow.now().format('YYYYMMDD-HHmmss-SSSSSS')
        self.stock = stock_symbol
        self.direction = direction
        self.price = price
        self.volume = volume
        self.status = status

    def process(self,):
        """"""

    def get_info(self,):
        """"""
        return {
            'orderid': self.orderid,
            'stock': self.stock,
            'direction': self.direction,
            'price': self.price,
            'volume': self.volume,
            'status':self.status
        }

    def set_status(self, status):
        """
        处理订单后设置订单状态
        """
        assert status in [SUBMIT, TRADE, CANCEL, REJECT]
        self.status = status

        return self.get_info()

    def reset_price(self, price):
        """
        当价格溢出时，以市场最高或者最低价成交
        """
        self.price = price

        return self.get_info()


class QuotationManager(object):
    """
    股价行情管理器，关注多资产的股价量价信息
    """
    def __init__(self,  config, 
                        calender, 
                        stock_history, 
                        window_len=32,
                        start_trade_date=None,
                        stop_trade_date=None,
                        prediction_history=None,
                        ):
        """
        参数：
            config, 配置文件
            calender, 交易日历
            stock_history, 历史数据
            window_len, 历史数据窗口

            predict_history,预测的历史与行情数据相同的处理方式
        """
        self.config = config
        self.calender = calender
        self.stock_history = stock_history
        self.window_len = window_len
        self.stop_trade_date = stop_trade_date
        self.stock_list = config['data']['stock_code']
        self.quotation_col = config['data']['daily_quotes']
        self.date_col = config['data']['date_col']
        self.target_col = config['data']['target']
        self.train_pct = config['preprocess']['train_pct']
        
        # prediction数据和列名
        self.prediction_history = prediction_history
        col_names = []
        for i in range(config['preprocess']['predict_len']):
            col_name = 'pred_' + str(i)
            col_names.append(col_name)
        self.prediction_col = col_names + ['epoch_loss', 'epoch_val_loss', 'epoch_acc', 'epoch_val_acc']

        assert len(self.stock_list) == len(self.stock_history)

        self.data_pro = DataProcessor(  date_col=self.date_col,
                                        daily_quotes=self.quotation_col,
                                        target_col=self.target_col,
                                        window_len=self.window_len,
                                        pct_scale=config['preprocess']['pct_scale'])

        # 定义存放行情信息的字典
        self.stock_quotation = {}
        for name,data in self.stock_history.items():
            # 计算日行情
            daily_quotes = self.data_pro.cal_daily_quotes(data)
            self.stock_quotation[name] = daily_quotes

        self._reset(start_trade_date)
        
    def _step(self, step_date,):
        """
        向前迭代一步，按照step date产生一个window的数据窗口

        参数：
            step_date,迭代步的日期索引
        """
        self.current_date = step_date

        quotation = self.get_window_quotation(self.current_date)
        prediction = self.get_prediction(self.current_date)
        high_low_price = self.get_high_low_price(self.current_date)

        done = bool(step_date > self.stop_trade_date)

        info = {
            "current_date":self.current_date,
            "high_low_price": high_low_price,
            "done":done
        }
        self.infos.append(info)

        return quotation, prediction, info


    def _reset(self, step_date):
        """"""
        self.current_date = step_date
        quotation = self.get_window_quotation(self.current_date)
        prediction = self.get_prediction(self.current_date)
        high_low_price = self.get_high_low_price(self.current_date)
        done = bool(step_date > self.stop_trade_date)

        info = {
            "high_low_price": high_low_price,
            "done":done
        }
        self.infos = [info, ]

        self.quotation_shape = quotation.shape
        self.prediction_shape = prediction.shape

        return quotation, prediction, self.infos


    def get_window_quotation(self, current_date):
        """
        获取股价行情，时间范围：[current_date - window_len, current_date]
        """
        window_quotation = []
        window_start = [i for i in self.calender if i <= current_date][-self.window_len]
        for k,v in self.stock_quotation.items():
            try:
                quote = v[v.index >= window_start].iloc[:self.window_len]
            except Exception as e:
                print(e)

            window_quotation.append(quote.values)

        if self.window_len == 1:
            window_quotation = np.array(window_quotation).reshape((len(window_quotation), v.shape[-1]))

        return window_quotation.T
    
    def get_prediction(self, current_date):
        """
        获取预测：[current_date, current_date + predict_len]
        """
        prediction_list = []
        for k,v in self.prediction_history.items():
            try:
                prediction = v[v.index > current_date].iloc[0]
            except Exception as e:
                print(e)

            # 将有效数据列放入window中
            prediction_list.append(prediction[self.prediction_col].values)
        
        # 横向拼接行情数据
        return np.array(prediction_list).T

    def get_high_low_price(self, current_date):
        """
            获取当日的最高最低价，用于计算订单
        """
        high_low_price = {}
        for k,v in self.stock_quotation.items():
            try:
                high_low = v[['daily_high', 'daily_low', 'daily_open', 'daily_close']].loc[current_date]
            except Exception as e:
                print(e)
            high_low['stock'] = k
            high_low_price[k] = high_low

        return high_low_price


class PortfolioManager(object):
    """
    资产管理器，提供Gym环境的资产向量，回测情况下，通过行情历史计算，实盘情况下，通过交易接口获取账户信息
    """
    def __init__(self,  config, 
                        calender, 
                        stock_history, 
                        init_asset=100000,
                        tax_rate=0.0025,
                        start_trade_date=None,
                        stop_trade_date=None,
                        window_len=32,
                        save=True
                        ):
        """
        参数：
            config, 配置文件
            calender, 交易日历
            stock_history, 历史数据
            init_asset，初始化资产（现金）
            tax_rate，交易税率
            start_trade_date，开始交易时间
            window_len，观察窗口长度

        """
        self.config = config
        self.calender = calender
        self.stock_history = stock_history
        self.stock_list = config['data']['stock_code']
        self.init_asset = init_asset
        self.tax_rate = tax_rate
        self.stop_trade_date = stop_trade_date
        self.window_len = window_len
        self.save = save

        self.date_col = config['data']['date_col']
        self.target_col = config['data']['target']

        self.n_asset = len(self.stock_list)

        self.metadata = {'render.modes':['human',]}

        self._reset(start_trade_date)


    def _step(self, offer, W, high_low, step_date):
        """
        每个交易期的资产量变化，一步迭代更新状态

        参数：
            offer,W1：agent计算出的报价向量和分配向量, 报价向量是波动的百分比
            trade_date:交易日期

        步骤：
            1.更新行情：获取最新行情并生成新的数据窗口，并且获得当日的最高最低价用于订单计算
            2.更新订单列表：根据高低价计算当日订单成交情况，清空列表
            3.更新资产组合：根据订单成交情况，更新资产向量，计算新的订单，并加入订单列表

        订单：
            1.买卖标的
            2.方向：买卖
            3.价格
            4.交易量
            5.状态

        """
        # 昨日价格、持有量、资产和权重
        P0 = self.P0
        V0 = self.V0
        A0 = self.A0
        W0 = self.W0

        V1 = V0
        # 对算法得出的W进行归一化，防止除以0
        W = W / (W.sum() + 1e-7)

        # 获取今日价格
        P1 = self.get_price_vector(step_date)

        # 首先处理存量订单，计算手续费，更新V1
        for stock,order in self.order_list.items():
            # 订单状态为[SUBMIT,REJECT]两种
            assert isinstance(order, Order)
            processed_order = self.order_process(order, high_low[stock])
            processed_info = processed_order.get_info()
            # 处理过的订单状态应该只有 TRADE CANCEL REJECT三种，将无效订单略过
            if processed_info['status'] in [CANCEL, REJECT, SUBMIT]:
                continue
            # 只处理状态为TRADE的订单
            idx = self.stock_list.index(stock) + 1
            if processed_info['direction'] == SELL:
                # 卖出，资金增加，持有量减少，收税
                V1[0] = V0[0] + processed_info['volume'] * processed_info['price'] * (1 - self.tax_rate)
                V1[idx] = V0[idx] - processed_info['volume']
            elif processed_info['direction'] == BUY:
                # 买入，资金减少，持有量增加，收税
                V1[0] = V0[0] - processed_info['volume'] * processed_info['price'] * (1 + self.tax_rate)
                V1[idx] = V0[idx] + processed_info['volume']
            
        # 保存订单历史
        order_history = self.order_list
        # 更新A1 W1
        A1 = P1 * V1
        W1 = A1 / A1.sum()

        # 输出成交的订单情况
        for stock,his_order in order_history.items():
            order_info = his_order.get_info()
            if order_info['status'] == TRADE:
                self.print_order(order_info, V1 * P1, step_date)

        # 清空订单列表
        self.order_list = {}

        # 出价是在今日的价格基础上乘以 (1+offer向量)
        offer_price = P1 * offer / 100 + P1
        offer_price = np.round(offer_price, 2)
        # 需要交易的资产数
        delta_A = (W - W1) * A1.sum()

        # 今日仓位
        position = V1[0]

        # 下新的订单，使用offer价格，次日生效
        assert len(delta_A) == len(self.stock_list) + 1

        # 计算订单，计算顺序为随机的，避免头部的资产频繁交易但尾部资产无法交易
        trade_tuple = [i for i in zip(self.stock_list, delta_A[1:], offer_price[1:], V1[1:])]
        random.shuffle(trade_tuple)

        for stock_i, delta_A_i, Offer_i, V_i in trade_tuple:
            # 买卖方向，使用long表示买 使用short表示卖
            direction = BUY if delta_A_i > 0 else SELL
            volume = round(abs(delta_A_i)/(Offer_i * 100)) * 100
            price = Offer_i
            # 订单合理性判断，避免出现成交量为0的订单
            if volume > 0:
                if direction == SELL:
                    # 卖出的量不能大于持仓
                    volume = V_i if volume > V_i else volume
                    order = Order(stock_symbol=stock_i, direction=SELL, price=price, volume=volume, status=SUBMIT)
                    self.order_list[stock_i] = order
                # 买入股票需要由足够的position
                if direction == BUY:
                    if position >= price * volume: # 资金足够
                        order = Order(stock_symbol=stock_i, direction=BUY, price=price, volume=volume, status=SUBMIT)
                        self.order_list[stock_i] = order
                        position = position - price * volume
                    else:
                        # 资金不够，被拒绝的订单也增加到列表中，便于记录
                        order = Order(stock_symbol=stock_i, direction=BUY, price=price, volume=volume, status=REJECT)
                        self.order_list[stock_i] = order

        # 尝试的总步数
        steps = len(self.infos) + 1

        # log奖励函数,
        reward = np.log(A1.sum()/A0.sum())
        # 积累奖励函数，经验证，需要使用积累奖励函数才能学到
        accumulated_reward = A1.sum()/self.init_asset
        
        # 含有势能函数的积累奖励，势能是本次step获得的奖励增益
        accumulated_reward_with_potential = accumulated_reward + np.log(1 + (A1.sum() - A0.sum()) / A0.sum())

        # 计算风险指标：最大回撤和夏普比率
        accumulated_reward_list = [i['accumulated_reward'] for i in self.infos] + [accumulated_reward]
        # 夏普
        sharpe_of_reward = sharpe(accumulated_reward_list)
        # 最大回撤
        mdd_of_reward = max_drawdown(accumulated_reward_list)
        # 衰减系数： 
        # 0.999 在200步时衰减为0.81， 0.998 在200步时衰减为0.67， 0.995在200步时衰减为0.366
        # 0.995 在100步时衰减为0.60， 0.99 在100步衰减为0.366
        gamma = 0.99

        # 含有MDD风险指标的奖励函数，积累奖励为带积累奖励函数
        accumulated_reward_with_mdd = accumulated_reward / (1 + mdd_of_reward * gamma ** steps)

        # 目标导向奖励 1.6为60%的收益率
        # target = self.config['training']['target_reward']
        # 积累奖励=1时，target reward=0;积累奖励=1+target时,target reward=1；积累奖励>1+target时,target reward<1
        '''
        if accumulated_reward <= 1 + target:
            self.target_reward = (accumulated_reward - 1) / target
        else:
            self.target_reward = 1 / (accumulated_reward - target)
        '''


        info = {
            "order_history":order_history,          # 今日成交订单
            "order_list":self.order_list,           # 计划明日执行的订单
            "position":position,
            "total_asset":A1.sum(),
            "reward":reward,
            "accumulated_reward":accumulated_reward,
            "accumulated_reward_with_potential":accumulated_reward_with_potential,
            "sharpe_of_reward":sharpe_of_reward,
            "mdd_of_reward":mdd_of_reward,
            "accumulated_reward_with_mdd":accumulated_reward_with_mdd,
            # "target_reward":self.target_reward,
            "asset_vector":{
                            "P1":P1, 
                            "V1":V1, 
                            "A1":A1, 
                            "W1":W1
                            },
            "asset_history":{
                            "P0":self.P0,
                            "V0":self.V0,
                            "A0":self.A0,
                            "W0":self.W0
                            }
        }

        # 打印资产向量，逢5日打印一次
        if step_date.day % 5 == 0:
            print("日期：%s" %step_date.strftime('%Y%m%d'))
            self.print_portfolio(info)

        # 如果损失大于阈值，则中断
        if accumulated_reward < 0.9:
            info['done'] = True
        else:
            info['done'] = False

        self.infos.append(info)

        self.P0 = P1
        self.A0 = A1
        self.W0 = W1
        self.V0 = V1

        return P1, V1, W1, info

    def _reset(self, step_date):
        """
        初始化资产向量和持有量
        """
        info = {}
        # 存储额外信息的全局infos
        self.infos = []
        # 订单列表，存储次日的订单
        self.order_list = {}
        # 定义价格向量
        self.P0 = self.get_price_vector(step_date)
        # 定义持有量向量
        self.V0 = np.array([self.init_asset] + [0.0] * self.n_asset)
        # 定义总资产
        self.A0 = self.P0 * self.V0
        # 定义资产分配比例
        self.W0 = self.A0 / self.A0.sum()

        return self.P0, self.V0, self.W0, info

    def get_price_vector(self, current_date):
        """
        获取指定日期的价格向量
        """
        price_list = []
        for stock, history in self.stock_history.items():
            try:
                price = history[self.target_col].loc[current_date]
            except Exception as e:
                print(e)
            price_list.append(price)

        P = np.array([[1.0] + price_list]).reshape((-1))

        return P
        
    def order_process(self, order:Order, high_low):
        """
        处理订单

        订单状态说明：
            买单如果资金不足，被标记为REJECT（拒单）
            卖单和资金充足的买单，提交的状态为SUBMIT
            判断报价不合理的，标记为CANCEL（已撤销）
            SUBMIT的订单，报价在最高最低价之间，合理成交之后，标记为TRADE
        """
        process_order = order
        info = order.get_info()
        if info['status'] == SUBMIT:
            if info['direction'] == BUY:
                # 买入的订单，报价高于最低价，就可以买入；
                if info['price'] >= high_low['daily_low']:
                    process_order.set_status(TRADE)
                    if info['price'] >= high_low['daily_high']:
                        # 买入订单报价，如果高于最高价，则以最高价成交
                        process_order.reset_price(high_low['daily_high'])
                else:
                    # 买入但报价低于最低价，不成交
                    process_order.set_status(CANCEL)
            else :
                # 卖出的订单，报价低于最高价，就可以卖出
                if info['price'] <= high_low['daily_high']:
                    process_order.set_status(TRADE)
                    if info['price'] <= high_low['daily_low']:
                        # 卖出的订单如果低于最低价，则以最低价成交
                        process_order.reset_price(high_low['daily_low'])
                else:
                    # 卖出但报价高于最高价，不成交
                    process_order.set_status(CANCEL)
        elif info['status'] == REJECT:
            pass

        return process_order

    def print_order(self, order_info, A, step_date):
        """
        打印订单信息
        """
        orderid = order_info['orderid']
        stock = order_info['stock']
        direction = "BUY  " if order_info['direction'] == BUY else "SELL "
        price = order_info['price']
        volume = order_info['volume']
        status = "SUCCESS" if order_info['status'] == TRADE else "CANCEL" if order_info['status'] == CANCEL else "REJECT"

        print(
            "日期 : " + step_date.strftime("%Y-%m-%d") + "|",
            #"订单号 : " + orderid + "|",
            "股票 : " + stock + "|",
            "买卖方向 : " + direction + "|",
            "报价 : " + str('%.2f' %price) + "|",
            "交易额 : " + str('%.2f' %(volume * price)) + "|",
            # "订单状态 : " + status + "|",
            "总资产 :  " + str('%.2f' %A.sum()) + "|"
        )

    def print_portfolio(self, info):
        """"""
        total = info['total_asset']
        portfolio = info['asset_vector']['A1']
        volume = info['asset_vector']['V1']
        price = info['asset_vector']['P1']
        asset_list = ['Position'] + self.stock_list

        assert len(portfolio) == len(asset_list)
        for a,p,v,pr in zip(asset_list, portfolio, volume, price):
            print(a, ':\t', str(p), '\t 持有量：', v, '\t 现价：', pr)
        print('Total: %f' %total)



class Portfolio_Prediction_Env(gym.GoalEnv):
    """
    基于股市预测的组合资产管理模拟环境

    GoalEnv适用于奖励稀疏的环境，

    """
    metadata = {'render.modes': ['human']}

    def __init__(self, config, 
                    calender, 
                    stock_history, 
                    prediction_history, 
                    init_asset=100000.0,
                    tax_rate=0.001,
                    window_len=1, 
                    start_trade_date=None,
                    stop_trade_date=None,
                    save=True):
        """
        参数：
            config, 配置文件
            calender, 交易日历 datetime对象的list
            stock_history, 股价历史数据
            prediction_history，预测历史数据

        说明：
            1.模拟环境在交易日收盘之后运行，预测未来价格，并做出投资决策
            2.每个step，首先处理上次step增加的订单，或是成交或者退出资金
            3.成交的订单，计算手续费后，加入总资产
            4.清空订单列表后，计算本次订单，加入列表
            5.冻结资金一并算入总资产
            6.未来与vnpy的回测引擎对接，可以直接包装为一个backtester类，读取每日的订单并下单
        """
        super(Portfolio_Prediction_Env, self).__init__()

        self.config = config
        self.stock_list = config['data']['stock_code']
        # 将calender转换为datetime
        self.calender = [i.date() for i in calender]
        
        # 将history中的索引转换为calender
        self.stock_history = {k:v.rename(index=pd.Series(self.calender)) for k,v in zip(self.stock_list, stock_history)}
        self.prediction_history = prediction_history
        self.window_len = window_len
        self.n_asset = len(stock_history)
        self.init_asset = init_asset
        self.tax_rate = tax_rate

        # 指定交易开始时间，默认以配置文件设定的比例开始，最好是在历史数据中随机开始
        if start_trade_date is None:
            self.decision_daterange = self.calender[int(len(self.calender) * config['preprocess']['train_pct']) + self.window_len :]
        elif isinstance(start_trade_date, str):
            self.decision_daterange = [i for i in self.calender if i >= arrow.get(start_trade_date, 'YYYYMMDD').date()]
        else:
            self.decision_daterange = [i for i in self.calender if i >= start_trade_date]
            
        # 指定交易结束时间，设定最大训练时长为200天
        if stop_trade_date is None:
            # 为指定停止训练的时间，默认一个交易年
            self.decision_daterange = self.decision_daterange[:200]
        elif isinstance(stop_trade_date, str):
            self.decision_daterange = [i for i in self.decision_daterange if i <= arrow.get(stop_trade_date, 'YYYYMMDD').date()]
        else:
            self.decision_daterange = [i for i in self.decision_daterange if i <= stop_trade_date]
        
        self.save = save

        self.quotation_mgr = QuotationManager(  config=config,
                                                calender=self.calender,
                                                stock_history=self.stock_history,
                                                window_len=window_len,
                                                prediction_history=prediction_history,
                                                start_trade_date=self.decision_daterange[0],
                                                stop_trade_date=self.decision_daterange[-1]
                                                )
        
        self.portfolio_mgr = PortfolioManager(  config=config,
                                                stock_history=self.stock_history,
                                                calender=self.calender,
                                                window_len=window_len,
                                                start_trade_date=self.decision_daterange[0],
                                                stop_trade_date=self.decision_daterange[-1],
                                                save=save)
        # 定义行为空间，offer的scale为100
        action_space_shape = [(self.n_asset + 1) * 2,]
        action_space_low = np.array([0.0] * (self.n_asset + 1) + [-10.0]* (self.n_asset + 1))
        action_space_high = np.array([1.0] * (self.n_asset + 1) + [10.0]* (self.n_asset + 1))
        self.action_space = Box(low=action_space_low, high=action_space_high, )

        # 定义观察空间：(行情+预测, n_asset)
        obs_space_shape = [(self.quotation_mgr.quotation_shape[0] + self.quotation_mgr.prediction_shape[0]) * self.n_asset]
        obs_space_low = -100 * np.ones(shape=obs_space_shape)
        obs_space_high = 100 * np.ones(shape=obs_space_shape)

        # 假设资产收益的上限是10倍
        # 已经达到的目标：At
        achieved_goal_shape = (self.n_asset + 1)
        achieved_goal_low = np.zeros(shape=achieved_goal_shape)
        achieved_goal_high = 10 * np.ones(shape=achieved_goal_shape)

        # 计划达到的目标：target reward * A0
        desired_goal_shape = (self.n_asset + 1, )
        desired_goal_low = np.zeros(shape=desired_goal_shape)
        desired_goal_high = (1 + self.config['training']['target_reward']) * np.ones(shape=desired_goal_shape)

        # 使用GoalEnv时，需要定义achieved_goal，和desired_goal，仅在HER算法下使用
        if self.config['training']['env_mode'] == 'goal':
            self.observation_space = Dict({
                'observation':Box(low=obs_space_low, high=obs_space_high,), 
                'achieved_goal':Box(low=achieved_goal_low, high=achieved_goal_high, ),
                'desired_goal': Box(low=desired_goal_low, high=desired_goal_high, ),
            })
        else:
            # DDPG TD3
            self.observation_space = Box(low=obs_space_low, high=obs_space_high,)
        
        self.reset()


    def step(self, action,):
        """
        环境中前进一步，保存历史
        """
        W = action[:self.n_asset + 1]
        offer = action[- self.n_asset - 1:]

        step_date = [i for i in self.calender if i > self.current_date][0]

        quotation, prediction, info1 = self.quotation_mgr._step(step_date)

        P1, V1, W1, info2 = self.portfolio_mgr._step(offer, W, info1['high_low_price'], step_date)

        obs = np.vstack((quotation, prediction)).reshape(-1)
        achieved_goal = P1 * V1

        observation = {
            'observation':obs,
            'achieved_goal':achieved_goal,
            'desired_goal': self.desired_goal
        }

        info = dict(info1, **info2)
        self.current_date = step_date
        info['current_date'] = self.current_date
        if info['current_date'] >= self.decision_daterange[-1]:
            info['done'] = True

        info['target_reward'] = reward_func(info['accumulated_reward'], self.config['training']['target_reward'])

        self.infos.append(info)

        # 在Goal环境下，需要返回reward target作为奖励函数
        if self.config['training']['env_mode'] == 'goal':
            return observation, info['target_reward'], info['done'], info
        else:
            return obs, info['target_reward'], info['done'], info


    def reset(self,):
        """"""
        self.current_date = self.decision_daterange[0]
        quotation, prediction, info1 = self.quotation_mgr._reset(self.current_date)
        P, V, W, info2 = self.portfolio_mgr._reset(self.current_date)
        info = dict(info1, **info2)
        
        self.order_list = {}
        self.infos = [info]

        obs = np.vstack((quotation, prediction)).reshape(-1)

        achieved_goal = P * V
        self.desired_goal = (self.config['training']['target_reward']) * achieved_goal

        observation = {
            'observation':obs,
            'achieved_goal':achieved_goal,
            'desired_goal': self.desired_goal
        }

        if self.config['training']['env_mode'] == 'goal':
            return observation
        else:
            return obs

    def compute_reward(self, achieved_goal, desired_goal, info):
        """
        使用achieved_goal，desired_goal计算出reward，必须与环境step中得到的reward一致
        """
        # 积累奖励=1时，target reward=0;积累奖励=1+target时,target reward=1；积累奖励>1+target时,target reward<1
        accumulated_reward = achieved_goal.sum() / self.init_asset

        reward = reward_func(accumulated_reward, self.config['training']['target_reward'])

        return reward


    def render(self, mode='human', info=None):
        """"""
        self.plot_notebook(info)

    def close(self,):
        """"""
        return self.reset()


    def save_history(self,):
        """
        保存交易历史等
        """
        st_list = self.stock_list

        # 统计表需要保存的列
        statistics_keys = ['current_date', 'position', 'total_asset', 'reward', 'accumulated_reward', 'done',
                            "accumulated_reward_with_potential","sharpe_of_reward","mdd_of_reward",
                            "accumulated_reward_with_mdd","target_reward"]
        # 订单表需要保存的列
        order_keys = ['current_date','orderid','stock','direction','price','volume','status']
        # 资产表需要保存的列
        portfolio_keys = ['current_date', 'P1', 'V1', 'W1', 'A1']

        statistics_df = pd.DataFrame()
        for info in self.infos:
            try:
                info_df = pd.DataFrame({k:v for k,v in info.items() if k in statistics_keys}, index=[info['current_date']])
                statistics_df = pd.concat((statistics_df, info_df), axis=0, ignore_index=False)
            except Exception as e:
                print(e)
                pass

        order_df = pd.DataFrame()
        for info in self.infos:
            try:
                for st,o in info['order_list'].items():
                    keys ={k:v for k,v in o.get_info().items()}
                    keys['direction'] = keys['direction'].value
                    keys['status'] = keys['status'].value
                    keys[order_keys[0]] = info[order_keys[0]]
                    info_df = pd.DataFrame(keys, index=[0])
                    order_df = pd.concat((order_df, info_df), axis=0, ignore_index=True)
            except Exception as e:
                print(e)
                pass

        portfolio_df = pd.DataFrame()
        for info in self.infos:
            try:
                flatten_keys = {}
                for vec,v in info['asset_vector'].items():
                    assert len(st_list) + 1 == len(v)
                    flatten_keys = dict({vec+'_'+name:element for name,element in zip(['position'] + st_list, v)}, **flatten_keys)
                info_df = pd.DataFrame(flatten_keys, index=[info['current_date']])
                portfolio_df = pd.concat((portfolio_df, info_df), axis=0, ignore_index=False)
            except Exception as e:
                print(e)

        now = arrow.now().format('YYYYMMDD-HHmmss')
        save_path = os.path.join(sys.path[0], 'output')
        tag = "from-" + self.decision_daterange[0].strftime("%Y%m%d") + '-to-' + self.decision_daterange[-1].strftime("%Y%m%d")
        statistics_df.to_csv(os.path.join(save_path, 'statistics_' + tag + '_' + now + '.csv'))
        order_df.to_csv(os.path.join(save_path, 'order_' + tag + '_' + now + '.csv'))
        portfolio_df.to_csv(os.path.join(save_path, 'portfolio_' + tag + '_' + now + '.csv'))

        print('Env trading status %s is saved to %s' %(tag, save_path))

        return statistics_df, order_df, portfolio_df

    def plot_notebook(self, close=False, info=None):
        """Live plot using the jupyter notebook rendering of matplotlib."""

        if close:
            self._plot = self._plot2 = self._plot3 = None
            return

        df_info = pd.DataFrame(info)
        df_info.index = pd.to_datetime(df_info["date"], unit='s')

        # plot prices and performance
        all_assets = ['BTCBTC'] + self.sim.asset_names
        if not self._plot:
            colors = [None] * len(all_assets) + ['black']
            self._plot_dir = os.path.join(
                self.log_dir, 'notebook_plot_prices_' + str(time.time())) if self.log_dir else None
            self._plot = LivePlotNotebook(
                log_dir=self._plot_dir, title='prices & performance', labels=all_assets + ["Portfolio"], ylabel='value', colors=colors)
        x = df_info.index
        y_portfolio = df_info["portfolio_value"]
        y_assets = [df_info['price_' + name].cumprod()
                    for name in all_assets]
        self._plot.update(x, y_assets + [y_portfolio])

        # plot portfolio weights
        if not self._plot2:
            self._plot_dir2 = os.path.join(
                self.log_dir, 'notebook_plot_weights_' + str(time.time())) if self.log_dir else None
            self._plot2 = LivePlotNotebook(
                log_dir=self._plot_dir2, labels=all_assets, title='weights', ylabel='weight')
        ys = [df_info['weight_' + name] for name in all_assets]
        self._plot2.update(x, ys)

        # plot portfolio costs
        if not self._plot3:
            self._plot_dir3 = os.path.join(
                self.log_dir, 'notebook_plot_cost_' + str(time.time())) if self.log_dir else None
            self._plot3 = LivePlotNotebook(
                log_dir=self._plot_dir3, labels=['cost'], title='costs', ylabel='cost')
        ys = [df_info['cost'].cumsum()]
        self._plot3.update(x, ys)

        if close:
            self._plot = self._plot2 = self._plot3 = None


class LivePlotNotebook(object):
    """
    Live plot using `%matplotlib notebook` in jupyter

    Usage:
    liveplot = LivePlotNotebook(labels=['a','b'])
    x = range(10)
    ya = np.random.random((10))
    yb = np.random.random((10))
    liveplot.update(x, [ya,yb])
    """

    def __init__(self, log_dir=None, episode=0, labels=[], title='', ylabel='returns', colors=None, linestyles=None, legend_outside=True):
        if not matplotlib.rcParams['backend'] == 'nbAgg':
            logging.warn(
                "The liveplot callback only work when matplotlib is using the nbAgg backend. Execute 'matplotlib.use('nbAgg', force=True)'' or '%matplotlib notebook'")

        self.log_dir = log_dir
        if log_dir:
            try:
                os.makedirs(log_dir)
            except OSError:
                pass
        self.i = episode

        fig, ax = plt.subplots(1, 1)

        for i in range(len(labels)):
            ax.plot(
                [0] * 20,
                label=labels[i],
                alpha=0.75,
                lw=2,
                color=colors[i] if colors else None,
                linestyle=linestyles[i] if linestyles else None,
            )

        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.set_xlabel('date')
        ax.set_ylabel(ylabel)
        ax.grid()
        ax.set_title(title)

        # give the legend it's own space, the right 20% where it right align left
        if legend_outside:
            fig.subplots_adjust(right=0.8)
            ax.legend(loc='center left', bbox_to_anchor=(
                1.0, 0.5), frameon=False)
        else:
            ax.legend()

        self.ax = ax
        self.fig = fig

    def update(self, x, ys):
        x = np.array(x)

        for i in range(len(ys)):
            # update price
            line = self.ax.lines[i]
            line.set_xdata(x)
            line.set_ydata(ys[i])

        # update limits
        y = np.concatenate(ys)
        y_extra = y.std() * 0.1
        if x.min() != x.max():
            self.ax.set_xlim(x.min(), x.max())
        if (y.min() - y_extra) != (y.max() + y_extra):
            self.ax.set_ylim(y.min() - y_extra, y.max() + y_extra)

        if self.log_dir:
            self.fig.savefig(os.path.join(
                self.log_dir, '%i_liveplot.png' % self.i))
        self.fig.canvas.draw()
        self.i += 1



def sharpe(returns, freq=250, rfr=0.02):
    """
    夏普比率
    
    """
    return (np.sqrt(freq) * np.mean(np.array(returns) - np.array(rfr))) / (np.std(np.array(returns) - np.array(rfr)) + 1e-7)


def max_drawdown(X):
    """
    最大回撤率
    """
    mdd = 0
    peak = X[0]
    for x in X:
        if x > peak:
            peak = x
        dd = (peak - x) / peak
        if dd > mdd:
            mdd = dd
    return mdd


def reward_func(x, reward):
    """
    设计了一个奖励函数y = f(x)，满足：
        1.在 x = reward 时，f(x) 取得最大值1，且只有这一个最大值
        2.在 x = 1时，f(x) = 0
        3.在定义域内全部平滑可导
        4.在 x < 1 时，f(x)为负数，导数为正数，而且随着x变小，导数变大
        5.在 x > reward 时，f(x) -> 0 ，恒为正数
    """
    w = (np.e - 1) * (x - 1)/ (reward - 1) + 1

    return np.e * np.log(w) / w
