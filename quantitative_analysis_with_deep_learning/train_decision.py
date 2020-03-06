import numpy as np 
import pandas as pd 
import arrow
import random

import gym
from stable_baselines import PPO2, DDPG, PPO1
from stable_baselines.common.noise import NormalActionNoise,OrnsteinUhlenbeckActionNoise, AdaptiveParamNoiseSpec
from stable_baselines.common.vec_env import DummyVecEnv
# from stable_baselines.common.policies import MlpPolicy
from stable_baselines.ddpg.policies import MlpPolicy
from portfolio_trade.env.custom_env import Portfolio_Prediction_Env, QuotationManager, PortfolioManager

from stable_baselines.common.env_checker import check_env

def train_decision( config=None, 
                    save=False, 
                    load=False, 
                    calender=None, 
                    history=None, 
                    predict_results_dict=None, 
                    test_mode=False):
    """
    训练决策模型，从数据库读取数据并进行决策训练

    参数：
        config:配置文件, 
        save：保存结果, 
        calender：交易日日历, 
        history：行情信息, 
        all_quotes:拼接之后的行情信息
        predict_results_dict：预测结果信息
    """
    # 首先处理预测数据中字符串日期
    predict_dict = {}
    for k,v in predict_results_dict.items():
        assert isinstance(v['predict_date'].iloc[0], str)
        tmp = v['predict_date'].apply(lambda x: arrow.get(x, 'YYYY-MM-DD').date())
        predict_dict[k] = v.rename(index=tmp)

    env = Portfolio_Prediction_Env( config=config,
                                    calender=calender, 
                                    stock_history=history, 
                                    window_len=1, 
                                    prediction_history=predict_dict,
                                    save=save)
    
    # 测试模式
    if test_mode:
        obs, _ = env.reset()

        # 

        for i in range(1000):
            W = np.random.uniform(0.0, 1.0, size=(6,))
            offer = np.random.uniform(-10.0, 10.0, size=(6,))
            obs, reward, info , done = env.step(offer=offer, W=W)
            # env.render()
            if done:
                env.save_history()
                break
        env.close()

    # 训练模式
    # env = DummyVecEnv(env) # PPO2需要向量化环境
    n_actions = env.action_space.shape
    param_noise = None
    action_noise = OrnsteinUhlenbeckActionNoise(mean=np.zeros(n_actions), sigma=float(0.5) * np.ones(n_actions))
    
    # 检查环境
    # check_env(env)
    
    if load:
        model = DDPG.load('DDPG')
    else:
        model = DDPG(   policy=MlpPolicy,
                        env=env,
                        verbose=1,
                        # param_noise=param_noise,
                        # action_noise=action_noise
                        )

    model.learn(total_timesteps=10000,)

    obs, _ = env.reset()

    for i in range(1000):
        action, _states = model.predict(obs)
        obs, reward, info , done = env.step(action[0], action[1])
        # env.render()
        if done:
            env.save_history()
            break

    env.close()


def order_process_trade():
    """
    处理订单（实盘环境）
    """

def connect_vnpy():
    """
    通过vnpy发送订单
    """
