"""
    全局训练流程：
        1.读取股票池，拉取行情，数据增量存储，
        2.读取账户，拉取资金、成交订单
        3.调用资产管理，计算资产向量
            (时间事件——决策时间)
            4.调用算法，训练：
                1.算法检测新数据，启动数据获取与处理
                2.传入资产向量、成交订单、股票数据到环境中
                3.训练t1批数据进行预测，得到预测向量
                4.训练t2批数据进行策略提升，得到交易向量
                5.返回交易向量到订单处理
            5.订单处理接收订单，处理并等待提交
            (时间事件——订单交易)
            6.提交订单
        7.更新资产向量
        8.循环

    环境：
        1.每个交易日之前，被冻结资金会自动返还（触发资产管理事件）
        2.手续费在成交时自动扣除
        3.奖励设置为一个投资周期（episode）的总资产增长率，投资周期为全局参数，默认63日（一季度，全年约250个交易日）
        4.评估方法还有最大回撤，夏普比率（Sharp）等

    问题：
        1.模型训练时，判断订单成交的依据：提交订单价格在high~low之内，否则不成交
        2.模型决策的内容，包括成交量和成交价，分别为Box行为，其中成交量单位-手，价格单位0.01元
        3.每次observe是延迟到次日才能得到结果（关于成交和冻结资金处理问题）
        4.配置文件在训练阶段作为参数传入各个组件中，而不是在组件定义时与配置文件绑定，减少耦合性

    配置文件：
        1.数据配置文件：记录最新数据时间，避免重复训练，记录数据时间索引，训练用参数文件索引等
        2.时序预测模型配置文件：记录超参数
        3.环境配置文件：记录环境配置参数，强化学习算法参数
"""
import json
import numpy as np 
import pandas as pd 
from sklearn.decomposition import PCA

from utils.data_manage import StockManager, PortfolioManager, DataDownloader
from utils.data_process import DataProcessor
from utils.order_process import OrderProcessor, TradeSimulator
from utils.data_process import DataProcessor


def prepare_train(config=None, download=False):
    """
    数据准备
    """
    data_cfg = config['data']

    # 初始化数据下载器 更新行情
    data_downloader = DataDownloader(data_path=data_cfg['data_dir'],
                                     stock_list_file=data_cfg['SH50_list_path'],
                                     )
    if download:
        data_downloader.download_stock(download_mode='additional',
                                       start_date=data_cfg['date_range'][0],
                                       date_col=data_cfg['date_col']
                                       )

    trade_calender = data_downloader.get_calender(start_date=data_cfg['date_range'][0])

    stock_mgr = StockManager(data_path=data_cfg['data_dir'],
                            stock_pool=data_cfg['stock_code'],
                            trade_calender=trade_calender,
                            date_col=data_cfg['date_col'],
                            quote_col=data_cfg['daily_quotes'])

    stock_mgr.global_preprocess()
    history = stock_mgr.get_history_data()
    all_quote = stock_mgr.get_quote()
    calender = stock_mgr.get_trade_calender()

    return calender, history, all_quote


def train_forecasting(config=None, save=False, calender=None, history=None):
    """
    训练预测模型
    """
    assert config is not None

    data_pro = DataProcessor(date_col=config['data']['date_col'],
                             daily_quotes=config['data']['daily_quotes'],
                             target_col=config['data']['target'])
    stock_list = config['data']['stock_code']
    assert len(stock_list) == len(history)
    # 对时间进行编码
    (date_list, embeddings_list) = data_pro.encode_date_embeddings(calender)

    # 预测结果的字典：索引、涨跌、方差
    predict_results_dict = {}

    # 对投资标的的历史数据进行建模
    for idx, data in zip(stock_list, history):

        # 计算技术指标、填充空值
        data_tec = data_pro.cal_technical_indicators(data, date_index=date_list)
        data_tec = data_pro.fill_nan(data_tec)

        # 计算傅里叶变换、填空
        data_fft = data_pro.cal_fft(data,)
        data_fft = data_pro.fill_nan(data_fft)

        # 计算日行情
        daily_quotes = data_pro.cal_daily_quotes(data)

        # 分离其他特征、填空
        daily_other_features = data_pro.split_quote_and_others(data)
        daily_other_features = data_pro.fill_nan(daily_other_features)

        assert data_tec.shape[0] == data_fft.shape[0] == daily_other_features.shape[0]

        # 将技术指标、傅里叶变换和除了额外指标进行拼接
        extra_features = np.concatenate([data_tec, data_fft, daily_other_features], axis=1).astype(float)
        
        # 处理无穷数
        extra_features_no_nan_inf = data_pro.fill_inf(extra_features)

        # 超限数量级，对数压缩
        scaled_extra_features = data_pro.convert_log(pd.DataFrame(extra_features_no_nan_inf), 
                                                         trigger=config['data']['log_threshold'])

        # 获取标签列
        real_price = daily_quotes.values[:, 0]
        if config['preprocess']['predict_type'] == 'real':
            y = real_price
        elif config['preprocess']['predict_type'] == 'diff':
            y = daily_quotes.values[:, 1]
        elif config['preprocess']['predict_type'] == 'pct':
            y = daily_quotes.values[:, 2]
        else:
            raise ValueError('Please input right prediction type: real/diff/pct .')

        # 建立时间和股价的索引，作为该数据集的全局索引，
        date_index = pd.to_datetime(date_list, format='%Y%m%d').date
        date_price_index = pd.DataFrame(np.concatenate([date_list.reshape((-1, 1)), real_price.reshape((-1, 1))],
                                  axis=1), columns=['date', 'price'], index=date_index)
        date_price_index['idx'] = range(len(date_price_index))

        # 拼接特征，顺序是[行情数据，时间编码，额外特征] ，标签是[标签]
        x = np.concatenate([daily_quotes.values, embeddings_list, scaled_extra_features.values], axis=1)

        # 确定训练集和测试集时间范围，模型在测试集中迭代训练并预测
        date_range_dict = data_pro.split_train_test_date(date_price_index=date_price_index,
                                                         train_pct=config['preprocess']['train_pct'],
                                                         validation_pct=config['preprocess']['validation_pct'])
        
        # 完整训练，保存训练参数


        # 迭代训练并预测，输出预测结果



    return pca_data


def forecast_model(config=None, mode='step or total'):
    """
    使用预测模型进行训练和预测
    """



def train_decision():
    """
    训练决策模型
    """

def trade_process():
    """
    处理订单
    """

def connect_vnpy():
    """
    通过vnpy发送订单
    """

def main():
    """"""
    with open('config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)
    # 准备交易行情和日历
    calender, history, all_quote = prepare_train(config, download=False)
    # 训练预测模型，得到预测向量和风险向量
    predict_price = train_forecasting(config, calender=calender, history=history)
    # 训练决策模型，初始化资金，得到



    print("A lot of work to do ...")


if __name__ == '__main__':
    main()