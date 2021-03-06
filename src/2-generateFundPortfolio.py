# -*- coding: utf-8 -*-
"""
Created on Sun Nov 25 16:16:10 2018

@author: JON7390
交易一期(基金)模型
"""
import itertools
import logging
import os
import pickle as pk
import sys
import time
from multiprocessing import Lock, Process, Queue, cpu_count

import cvxopt as opt
import numpy as np
import pandas as pd
from cvxopt import blas, solvers
from dateutil.relativedelta import relativedelta
from scipy.special import comb

from src.utils import DataProcess

solvers.options['show_progress'] = False  # Turn off progress printing
sys.path.append('../')

logging.basicConfig(level=logging.INFO,
                    filename='../log/2-genFundPortfolio.log',
                    filemode='w',
                    datefmt='%Y/%m/%d %H:%M:%S',
                    format='%(levelname)s %(asctime)s %(funcName)s %(lineno)d %(message)s'
                    )
logger = logging.getLogger('main')
logger.addHandler(logging.StreamHandler())


def sharpeRatio(df, optional_risk, risk_free, fit_frequency, single=False):
    perYear = 1
    if 'BQ' in fit_frequency:
        perYear = 4
    elif 'BM' in fit_frequency:
        perYear = 12
    risk_free_rate = (1 + risk_free) ** (1 / perYear) - 1
    if single == False:
        df['sharpeRatio'] = df.apply(lambda x:(x['returns']-risk_free_rate)/x['risks'],axis =1)
    elif single == True:
        df = (df-risk_free_rate)/optional_risk
    return df


def preciseCorvariance(nav): # calculate precise corvariance
    start = time.time()
    df = nav.copy()
    # del df['the_date']
    df = df.fillna(method='ffill').pct_change()
    corMat = pd.DataFrame(np.zeros(shape = [df.shape[1],df.shape[1]]),columns = df.columns,index = df.columns)
    corTotal = comb(df.shape[1],2)
    verbose = 500
    count = 0
    # each iteration
    for a, b in itertools.combinations(df.columns, 2):
        preCor = df[[a,b]]
        preCor = preCor[preCor.count(axis =1)==2].T.astype('float')
        # preCor = preCor.T.astype('float')
        # print(a,b)
        tmp = np.cov(preCor)[1,0]
        corMat.loc[a,b] = tmp
        corMat.loc[b,a] = tmp
        count = count+1
        if count%verbose ==0:
            logger.info("Calculating Covariance-[%d/ %f%%]..." % (count, count * 100 / corTotal))
    logger.info("\nCalculating Variance...")
    for c in itertools.combinations(df.columns, 1):
        preCor = df[c[0]].dropna()
        corMat.loc[c,c] = np.var(preCor.T)
    end = time.time()
    logger.info("Time used: %s\n" % (str(end - start)))
    return corMat


def fitHM(nav,frequency='BQ-DEC',ytg = 8):
    goback = nav.index[-1] - relativedelta(years = ytg)
    df = nav.copy()
    df = df.fillna(method='bfill').resample(frequency).asfreq().pct_change()
    # df = df.fillna(method='ffill').fillna(method='bfill').resample(frequency).asfreq()
    # roi = df.pct_change()
    # mode = 'train'
    roi = df.loc[goback:]
    return roi


def optimalPortfolio(navReturn, nbr, pre, risk_free, fit_frequency):  # harry markoviz optimizer
    # Convert to cvxopt matricess
    navReturn = np.asmatrix(navReturn.T.values)
    if isinstance(pre,pd.DataFrame):
        S = opt.matrix(np.asarray(pre))
    else:
        S = opt.matrix(np.cov(navReturn))  # S-> covariance matrix
    pbar = opt.matrix(np.mean(navReturn, axis=1)) # pbar -> expected returns

    # Create constraint matrices
    G = -opt.matrix(np.eye(nbr))   # negative nbr x nbr identity matrix
    h = opt.matrix(0.0, (nbr, 1))  # all weight >= 0
    A = opt.matrix(1.0, (1, nbr))
    b = opt.matrix(1.0) # weights sum up to 1

    #  scale desired returns
    N = 100
    mus = [10**(4 * t/N - 2) for t in range(N)] # desired portfolio returns

    # Calculate [efficient frontier] weights using quadratic programming
    portfolios = [solvers.qp(mu*S, -pbar, G, h, A, b)['x'] for mu in mus]  # x-> weighted portfolios| solvers.qp(P,q,G,h,A,b)

    # CALCULATE RISKS AND RETURNS FOR FRONTIER
    returns = [blas.dot(pbar, x) for x in portfolios]
    risks = [np.sqrt(blas.dot(x, S*x)) for x in portfolios]
    r_r = pd.DataFrame({'risks':risks,'returns':returns})

    # FIT THE 2ND DEGREE POLYNOMIAL OF THE FRONTIER CURVE
    m1 = np.polyfit(returns, risks, 2) # polyfit: x,y,degree

    # CALCULATE 3 OUTPUT:  MAXIMUM SHARPE RATIO/ DEFAULT/ MINIMUM RISK
    srp = sharpeRatio(r_r, None, risk_free, fit_frequency, False)
    srp_y = float(srp.loc[srp['sharpeRatio'] == srp['sharpeRatio'].max(), 'returns'])
    # srp_y = srp[0] if isinstance(srp_y,seri) else srp_y # TODO AVOID MULTIPLE RESULT
    min_y = -m1[1] / (2 * m1[0])  # -(b/2a)
    if (m1[2] / m1[0]) < 0:
        y1 = min_y
    else:
        y1 = np.sqrt(m1[2] / m1[0])
    port =[]
    for i in [srp_y,y1,min_y]:
        op_wt = solvers.qp(opt.matrix(i * S), -pbar, G, h, A, b)['x']  # CALCULATE THE [OPTIMAL PORTFOLIO WEIGHT] with min risk
        op_return = blas.dot(pbar, op_wt)  # return under OPTIMAL PORTFOLIO
        op_risk = np.sqrt(blas.dot(op_wt, S * op_wt))  # risk under OPTIMAL PORTFOLIO
        port.append( [np.asarray(op_wt),op_return,op_risk])
    return port


def calculating_proc(nbr, return_vec, preCor, in_queue, out_queue, lock_in, lock_out, risk_free,
                     fit_frequency):  # multiprocess -> input queue + function + output queue
    in_count = 0
    out_count = 0
    while in_queue.qsize():
        lock_in.acquire()
        col = in_queue.get()
        in_count = in_count + 1
        lock_in.release()
        epo_return = return_vec[list(col)]
        epo_cor = None
        if isinstance(preCor, pd.DataFrame):
            epo_cor = preCor.loc[list(col), list(col)]
        # train and output
        try:
            port = optimalPortfolio(epo_return, nbr, epo_cor, risk_free, fit_frequency)  # optimal solver
            lock_out.acquire()
            out_queue.put([col, port])
            out_count = out_count + 1
            lock_out.release()
        except ValueError:
            lock_out.release()
            logger.exception(str("\n本组合计算失败：%s" % str(col)))
    logger.info('[%s] in-%d | out-%d - exit calculating process' % (str(os.getpid()), in_count, out_count))


def formating_proc(nbr, train_date, out_queue, risk_free, fit_frequency):
    po = pd.DataFrame()
    for_count = 0
    awake = 0
    while True:
        if out_queue.empty():
            awake = awake + 1
            logger.info('%d time(s) to halt formatting process' % awake)
            time.sleep(3)
            if awake == 5:
                logger.info('[%s] format-%d - exit formatting process' % (str(os.getpid()), for_count))
                break
        else:
            awake = 0
            for_count = for_count + 1
            col_port = out_queue.get()
            col = col_port[0]
            port = col_port[1]
            port_ = pd.DataFrame()
            # deal w/ result
            fund = pd.DataFrame(list(col)).T.add_prefix('fundCode_')  # fund_code
            for idx, label in ([0, 'srp'], [1, 'def'], [2, 'min']):
                wt = pd.DataFrame(data=port[idx][0].T).add_prefix('portfolio_')
                port_ = pd.concat([port_, pd.DataFrame(pd.concat(
                    [fund, wt, pd.Series(port[idx][1]), pd.Series(port[idx][2]), pd.Series(label),
                     pd.Series(str(train_date)[:10])], axis=1))], axis=0, ignore_index=True)
            po = pd.concat([po, port_], axis=0, ignore_index=True)
            if for_count % 200 == 0:
                logger.info('formatting %d' % for_count)
    po.rename(columns={0: 'returns', 1: 'risks', 2: 'label', 3: 'train_date'}, inplace=True)
    po = sharpeRatio(po, None, risk_free, fit_frequency, False)
    po = po.round(3)
    po['unique_flag'] = po.apply(lambda x: int(x[0]) * x[3] + int(x[1]) * x[4] + int(x[2]) * x[5],
                                 axis=1)  # to perfect analysis stage
    po = po.iloc[po['unique_flag'].drop_duplicates().index, :-1]
    # po = po.drop('unique_flag', axis=1).drop_duplicates()
    po.to_csv('../out/3-portfolio_%d_%s.csv' % (nbr, str(time.strftime('%Y%m%d', time.localtime(time.time())))),
              index=False)


def harryMarkowitz(nbr, return_vec, preCor = None):
    global risk_free, fit_frequency, lock_in, lock_out
    in_queue = Queue()  # multiprocess -> input queue
    out_queue = Queue()  # multiprocess -> out_queue queue
    proc_list = []
    # fill input
    for col in itertools.combinations(return_vec.columns, nbr):
        in_queue.put(col)
    # creating processes
    start = time.time()
    for w in range(proc_nbr):
        p = Process(target=calculating_proc,
                    args=(nbr, return_vec, preCor, in_queue, out_queue, lock_in, lock_out, risk_free, fit_frequency))
        proc_list.append(p)
    proc_list.append(
        Process(target=formating_proc, args=(nbr, return_vec.index[-1], out_queue, risk_free, fit_frequency)))
    [p.start() for p in proc_list]
    # completing process
    [p.join() for p in proc_list]
    logger.info("\nTime used : " + str(time.time() - start))


def filterManager(mana_info,mana_chg, annual_return_score = 0.075, cum_on_duty_term_pct = 0.55, annual_return_fund= 0.075, term = 1.0, weighted_annual_return_score =0.075, mode ='loose'):
    mana_chg['manager_id'].replace({np.nan: 0}, inplace=True)

    # filterManager based on manager
    f1 = mana_info.loc[(mana_info['annual_return_score'] >= annual_return_score),['manager_name','manager_id']]  # filterManager 1
    f2 = mana_info.loc[(mana_info['cum_on_duty_term_pct'] >= cum_on_duty_term_pct), ['manager_name','manager_id']]  # filterManager 2
    # filterManager 1
    mana_chg['f1'] = mana_chg.apply(lambda x: 1 if (str(x['manager_id']) in list(f1['manager_id'])) else 0,axis =1)
    mana_chg.loc[mana_chg['manager_id']==0,'f1'] = mana_chg[mana_chg['manager_id']==0].apply(lambda x: 1 if (x['single_manager'] in list(f1['manager_name'])) else 0,axis =1)
    # filterManager 2
    mana_chg['f2'] = mana_chg.apply(lambda x: 1 if (str(x['manager_id']) in list(f2['manager_id'])) else 0,axis =1)
    mana_chg.loc[mana_chg['manager_id']==0,'f2'] = mana_chg[mana_chg['manager_id']==0].apply(lambda x: 1 if (x['single_manager'] in list(f2['manager_name'])) else 0,axis =1)

    # filterManager based on fund
    # filterManager 3
    mana_chg['f3'] = mana_chg.apply(lambda x: 1 if float(x['annual_return_fund']) >= annual_return_fund else 0,axis =1)
    # filterManager 4
    mana_chg['f4'] = mana_chg.apply(lambda x: 1 if float(x['term'])>= term else 0,axis =1)
    # filterManager 5
    mana_chg['f5'] = mana_chg.apply(lambda x: 1 if float(x['weighted_annual_return_score'])>= weighted_annual_return_score else 0,axis =1)

    # check
    for i in list(['f1','f2','f3','f4','f5']):
        logger.info('After filterManager %s:'%i + str(pd.Series(mana_chg.loc[mana_chg[i]>=1,'fund_code'].unique()).count()))
    fund_list =pd.DataFrame()
    if mode =='loose':
        fund_list = mana_chg.loc[((mana_chg['f1']+mana_chg['f2']+mana_chg['f3']+mana_chg['f4']>=4) | (mana_chg['f2']+mana_chg['f3']+mana_chg['f4']+mana_chg['f5']>=4)),:]
    elif mode =='strict':
        fund_list = mana_chg.loc[(mana_chg['f1'] + mana_chg['f2'] + mana_chg['f3'] + mana_chg['f4'] + mana_chg['f5']>= 5), :]
    else:
        return 1
    logger.info('After ALL FILTER:'+str(len(fund_list['fund_code'].unique())))
    return fund_list['fund_code']


def main():
    global risk_free, fit_frequency, lock_in, lock_out, proc_nbr
    risk_free = 0.035
    fit_frequency = 'BM' # 'BQ-DEC'
    validation_period = 3
    portfolio_nbr=[3]

    lock_in = Lock()
    lock_out = Lock()
    proc_nbr = 6

    # load data
    nav = pd.read_csv('../out/1-fund_nav.csv', dtype={'fund_code': str})
    cur = pd.read_csv('../out/1-fund_nav_currency.csv', dtype={'fund_code': str})
    mana_his = pd.read_csv('../out/1-fund_managers_his.csv', dtype=str)
    mana_info = pd.read_csv('../out/1-fund_managers_info.csv', dtype=str)
    mana_chg = pd.read_csv('../out/1-fund_managers_chg.csv', dtype=str)

    # data process
    nav = DataProcess.processNAV(nav, fit_frequency, True)  # adjusted prices
    cur = DataProcess.processNAV(cur, fit_frequency, True)  # annualised return rate by frequency TODO MERGE IT
    mana_info, p_chg = DataProcess.processManager(mana_his, mana_info, mana_chg)  # manager

    # filter data
    fund_list = filterManager(mana_info, p_chg, annual_return_score=0.065, cum_on_duty_term_pct=0.60,
                              annual_return_fund=0.06,
                              term=1.5, weighted_annual_return_score=0.05,
                              mode='strict')  # fund list after filtering managers
    nav_train = nav[list(fund_list)]

    # to speed up dev stage
    pk.dump(nav_train, open('processed_nav.dat', 'wb'), True)
    # nav_train = pk.load(open('processed_nav.dat', 'rb'))


    # split data
    nav_valid = nav_train.iloc[-(validation_period+1):,:] # split data for validation
    nav_train = nav_train.iloc[:-(validation_period),:] # split data for modeling

    # training
    preCor = preciseCorvariance(nav_train) # gen precise corvariance
    return_vec = fitHM(nav_train, fit_frequency,
                       10)  # gen return of interest: data, resample Frequency, years to go back
    logger.info('\n%d CPU on board, starting %d process' % (cpu_count(), proc_nbr))
    for nbr in portfolio_nbr:
        harryMarkowitz(nbr, return_vec, preCor)

    # validation
    nav_valid = nav_valid.iloc[:-1, :]  # get rid of data not reaching to the end of the month
    valid_date = nav_valid.index[-1]
    nav_valid = (1+nav_valid.fillna(method='bfill').resample(fit_frequency).asfreq().pct_change()).cumprod().iloc[-1,:]-1
    for nbr in portfolio_nbr:
        res_valid = pd.DataFrame()
        port = pd.read_csv(
            '../out/3-portfolio_%d_%s.csv' % (nbr, str(time.strftime('%Y%m%d', time.localtime(time.time())))),
            dtype=dict(('fundCode_' + str(i), 'str') for i in range(nbr))).round(3)
        for i in range(len(port)):
            slice =port.iloc[i,:]
            fundCode = slice[:nbr]
            val = nav_valid[list(fundCode)]
            wt = slice[3:6]
            slice = slice.append(pd.Series(np.dot(wt, val), index=['validation']))
            slice = slice.append(pd.Series(str(valid_date)[:10], index=['valid_date']))
            res_valid = pd.concat([res_valid, slice.to_frame().T, ], axis=0)
        res_valid = res_valid.round(3)
        res_valid.to_csv(
            '../out/3-validation_%d_%s.csv' % (nbr, str(time.strftime('%Y%m%d', time.localtime(time.time())))),
            index=False)

if __name__ == "__main__":
    main()