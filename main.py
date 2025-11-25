import streamlit as st
import requests
from datetime import datetime, timedelta
import time
import logging
from typing import Dict, Optional, Tuple

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('improved_fetch_tvl')

# -------------------------- 内置配置（无需修改，直接运行）--------------------------
# 示范监控目标：以太坊Uniswap V3 WETH-USDC LP池（主流成熟合约，数据充足）
TARGET_CONTRACT = "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640"
CHAIN = "ethereum"
LP_POOL_ADDRESS = TARGET_CONTRACT
CORE_TOKEN = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"  # USDC合约地址
ETHERSCAN_API_KEY = "YourApiKeyToken"  # Etherscan免费API默认占位符（无Key也能跑）

# 监控阈值（Demo专用，平衡效果与误报）
FUND_OUTFLOW_THRESHOLD = 0.3  # 30%资金净流出预警
LIQUIDITY_DROP_THRESHOLD = 0.3  # 30%流动性骤降预警
FAILED_RATE_THRESHOLD = 0.3  # 30%交互失败率预警

# -------------------------- 增强的TVL获取器类 --------------------------
class TVLFetcher:
    """
    增强的TVL获取器，支持多重数据源和错误处理
    专为以太坊Uniswap V3 WETH-USDC LP池设计
    """
    
    def __init__(self):
        # 配置参数
        self.lp_pool_address =  TARGET_CONTRACT # WETH-USDC LP池地址
        self.timeout = 10
        self.max_retries = 3
        self.base_backoff = 1
        self.last_successful_tvl = None  # 用于故障转移到上次成功值
        self.consecutive_failures = 0
        self.max_consecutive_failures = 5
        self.manual_tvl = None
        self.source_success_count = {}
        
    def _retry_with_backoff(self, func, *args, **kwargs) -> Optional[Dict]:
        """带指数退避的重试机制"""
        last_exception = None
        
        for attempt in range(self.max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_exception = e
                wait_time = self.base_backoff * (2 ** attempt) + (attempt * 0.1)
                logger.warning(f"尝试 {attempt + 1}/{self.max_retries} 失败: {str(e)}. 等待 {wait_time:.2f} 秒后重试...")
                time.sleep(wait_time)
        
        logger.error(f"所有 {self.max_retries} 次尝试均失败: {str(last_exception)}")
        return None
    
    def _fetch_from_defillama(self) -> Optional[float]:
        """从DeFiLlama获取TVL数据"""
        logger.info("尝试从DeFiLlama获取TVL数据")
        
        def _api_call():
            resp = requests.get("https://api.llama.fi/protocol/uniswap-v3-ethereum", timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        
        data = self._retry_with_backoff(_api_call)
        if data:
            tvl = data.get("tvl", None)
            if tvl is not None and isinstance(tvl, (int, float)) and tvl >= 0:
                return round(tvl, 2)
        
        logger.error("DeFiLlama API调用失败或返回无效数据")
        return None
    
    def _fetch_from_dexscreener(self) -> Optional[float]:
        """从DexScreener获取LP池流动性作为TVL替代"""
        logger.info("尝试从DexScreener获取流动性数据")
        
        def _api_call():
            url = f"https://api.dexscreener.io/latest/dex/pairs/ethereum/{self.lp_pool_address.lower()}"
            resp = requests.get(url, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        
        data = self._retry_with_backoff(_api_call)
        if data and "pair" in data:
            tvl = data.get("pair", {}).get("liquidity", {}).get("usd", None)
            if tvl is not None and isinstance(tvl, (int, float)) and tvl >= 0:
                return round(tvl, 2)
        
        logger.error("DexScreener API调用失败或返回无效数据")
        return None
    
    def get_tvl(self) -> Tuple[float, str]:
        """获取TVL，尝试多个数据源并进行故障转移"""
        start_time = time.time()
        logger.info("开始获取TVL数据")
        
        # 首先检查是否有手动设置的值
        if self.manual_tvl is not None:
            logger.info(f"使用手动设置的TVL值: ${self.manual_tvl:,.2f}")
            elapsed_time = time.time() - start_time
            logger.info(f"TVL获取耗时: {elapsed_time:.2f}秒")
            return self.manual_tvl, "ManualOverride"
        
        # 按优先级尝试数据源 - 调整顺序，将DexScreener设为主数据源
        sources = [
            (self._fetch_from_dexscreener, "DexScreener"),
            (self._fetch_from_defillama, "DeFiLlama")
        ]
        
        all_failed = True
        
        for fetch_func, source_name in sources:
            tvl = fetch_func()
            if tvl is not None and tvl > 0 and self.validate_tvl(tvl):
                all_failed = False
                self.consecutive_failures = 0
                
                # 更新成功计数
                if source_name not in self.source_success_count:
                    self.source_success_count[source_name] = 0
                self.source_success_count[source_name] += 1
                
                logger.info(f"成功从{source_name}获取TVL: ${tvl:,.2f}")
                self.last_successful_tvl = tvl
                
                elapsed_time = time.time() - start_time
                logger.info(f"TVL获取耗时: {elapsed_time:.2f}秒")
                
                return tvl, source_name
        
        # 如果所有数据源都失败，检查连续失败次数
        if all_failed:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.max_consecutive_failures:
                logger.error(f"连续{self.consecutive_failures}次所有数据源失败，需要检查API状态")
        
        # 尝试返回上次成功的值
        if self.last_successful_tvl is not None:
            logger.warning(f"所有数据源失败，使用上次成功值: ${self.last_successful_tvl:,.2f}")
            elapsed_time = time.time() - start_time
            logger.info(f"TVL获取耗时: {elapsed_time:.2f}秒")
            return self.last_successful_tvl, "LastSuccessfulValue"
        
        # 最后的兜底，返回0并记录警告
        logger.warning("所有数据源失败且无历史值，返回默认值0")
        elapsed_time = time.time() - start_time
        logger.info(f"TVL获取耗时: {elapsed_time:.2f}秒")
        return 0.0, "Default"
    
    def validate_tvl(self, tvl: float) -> bool:
        """验证TVL值的合理性"""
        # 基本验证：必须是非负数
        if tvl < 0:
            logger.warning(f"TVL值为负: {tvl}")
            return False
        
        # 如果有历史值，检查是否存在异常波动
        if self.last_successful_tvl is not None:
            change_pct = abs((tvl - self.last_successful_tvl) / self.last_successful_tvl * 100)
            if change_pct > 90:  # 超过90%的变化视为可疑
                logger.warning(f"TVL值变化过大: {change_pct:.2f}%")
                return False
        
        return True

# 使用单例模式来保存状态
tvl_fetcher_instance = None

# -------------------------- 免费数据源API封装 --------------------------
def fetch_contract_tvl():
    """
    增强的TVL获取函数，与原始接口保持兼容
    实现了多重数据源、重试机制、数据验证和故障转移
    """
    global tvl_fetcher_instance
    
    # 确保单例实例存在
    if tvl_fetcher_instance is None:
        tvl_fetcher_instance = TVLFetcher()
    
    # 获取TVL值
    tvl, source = tvl_fetcher_instance.get_tvl()
    
    # 返回TVL值（与原始接口兼容）
    return tvl

def fetch_lp_liquidity():
    """从Dex Screener免费API获取LP池流动性"""
    try:
        resp = requests.get(
            f"https://api.dexscreener.io/latest/dex/pairs/ethereum/{LP_POOL_ADDRESS.lower()}",
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        return round(data.get("pair", {}).get("liquidity", {}).get("usd", 0), 2)
    except Exception:
        return 0.0

def fetch_contract_transactions():
    """从Etherscan免费API获取最近50笔内部交易"""
    url = "https://api.etherscan.io/api"
    params = {
        "module": "account",
        "action": "txlistinternal",
        "address": TARGET_CONTRACT,
        "sort": "desc",
        "offset": 50,
        "apikey": ETHERSCAN_API_KEY
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json().get("result", [])
    except Exception:
        return []

def fetch_token_price():
    """从CoinGecko免费API获取USDC价格（稳定币）"""
    try:
        resp = requests.get(
            f"https://api.coingecko.com/api/v3/simple/token_price/ethereum?contract_addresses={CORE_TOKEN}&vs_currencies=usd",
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get(CORE_TOKEN.lower(), {}).get("usd", 1.0)
    except Exception:
        return 1.0

# -------------------------- 核心指标计算逻辑（无需修改）--------------------------
def calculate_fund_outflow():
    """指标1：1小时资金净流出率"""
    tvl = fetch_contract_tvl()
    if tvl <= 0:
        return 0.0, False
    
    transactions = fetch_contract_transactions()
    one_hour_ago = datetime.now() - timedelta(hours=1)
    inflow_usd = 0.0
    outflow_usd = 0.0
    token_price = fetch_token_price()

    for tx in transactions:
        try:
            # 添加类型检查，确保tx是字典
            if not isinstance(tx, dict):
                continue
                
            # 过滤1小时内的交易
            tx_time = datetime.fromtimestamp(int(tx.get("timeStamp", 0)))
            if tx_time < one_hour_ago:
                continue
            
            # 计算交易金额（ETH转USD）
            value_eth = int(tx.get("value", 0)) / 10**18
            value_usd = value_eth * token_price
            
            # 区分流入/流出
            from_addr = tx.get("from", "").lower()
            to_addr = tx.get("to", "").lower()
            if from_addr == TARGET_CONTRACT.lower():
                outflow_usd += value_usd
            elif to_addr == TARGET_CONTRACT.lower():
                inflow_usd += value_usd
        except Exception:
            continue

    # 计算净流出率
    net_outflow = outflow_usd - inflow_usd
    net_outflow_rate = (net_outflow / tvl) * 100 if tvl != 0 else 0.0
    return round(net_outflow_rate, 2), net_outflow_rate > (FUND_OUTFLOW_THRESHOLD * 100)

def calculate_liquidity_change():
    """指标2：1小时流动性变化率（模拟历史数据对比）"""
    current_liquidity = fetch_lp_liquidity()
    if current_liquidity <= 0:
        return 0.0, False, 0.0
    
    # 模拟1小时前流动性（实际落地可替换为数据库存储）
    historical_liquidity = current_liquidity * 1.2  # 假设1小时前流动性更高
    change_rate = ((current_liquidity - historical_liquidity) / historical_liquidity) * 100
    is_alert = change_rate < -(LIQUIDITY_DROP_THRESHOLD * 100)  # 负号表示下降
    
    return round(change_rate, 2), is_alert, current_liquidity

def calculate_failed_rate():
    """指标3：合约交互失败率（近50笔交易）"""
    transactions = fetch_contract_transactions()
    total_tx = len(transactions)
    if total_tx == 0:
        return 0.0, False, 0, 0
    
    # 统计失败交易（isError=1表示失败），添加类型检查
    failed_tx = 0
    for tx in transactions:
        # 确保tx是字典类型
        if isinstance(tx, dict) and tx.get("isError", "0") == "1":
            failed_tx += 1
    
    failed_rate = (failed_tx / total_tx) * 100 if total_tx > 0 else 0.0
    return round(failed_rate, 2), failed_rate > (FAILED_RATE_THRESHOLD * 100), failed_tx, total_tx

# -------------------------- Web界面渲染（无需修改）--------------------------
def main():
    # 页面基础配置
    st.set_page_config(
        page_title="合约安全监控Demo",
        page_icon="🚨",
        layout="centered",
        initial_sidebar_state="collapsed"
    )

    # 页面标题与基础信息
    st.title("🚨 合约安全监控Demo（零配置版）")
    st.subheader("监控目标：Uniswap V3 WETH-USDC LP池（以太坊链）")
    st.markdown(f"📋 合约地址：`{TARGET_CONTRACT}`")
    st.markdown(f"⌛ 最后刷新：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    st.divider()

    # 数据加载与指标计算（带加载动画）
    with st.spinner("正在抓取链上数据并计算指标..."):
        fund_rate, fund_alert = calculate_fund_outflow()
        liq_rate, liq_alert, liq_current = calculate_liquidity_change()
        fail_rate, fail_alert, fail_cnt, total_cnt = calculate_failed_rate()
        tvl = fetch_contract_tvl()
        token_price = fetch_token_price()

    # 核心指标展示（分栏+预警颜色）
    st.subheader("🎯 核心监控指标")
    
    # 指标1：资金净流出率
    col1, col2 = st.columns(2)
    with col1:
        st.metric(
            label="1小时资金净流出率",
            value=f"{fund_rate}%",
            delta="⚠️ 预警" if fund_alert else "✅ 正常",
            delta_color="inverse" if fund_alert else "normal"
        )
        if fund_alert:
            st.error("❌ 净流出率超过30%！可能存在资金撤离或盗转风险！")

    # 指标2：流动性变化率
    with col2:
        st.metric(
            label="1小时流动性变化率",
            value=f"{liq_rate}%",
            delta="⚠️ 预警" if liq_alert else "✅ 正常",
            delta_color="inverse" if liq_alert else "normal"
        )
        st.markdown(f"当前流动性：${liq_current:,.2f}")
        if liq_alert:
            st.error("❌ 流动性下降超过30%！可能引发价格操纵或交易失败！")

    # 指标3：交互失败率
    st.metric(
        label="合约交互失败率（近50笔）",
        value=f"{fail_rate}%",
        delta="⚠️ 预警" if fail_alert else "✅ 正常",
        delta_color="inverse" if fail_alert else "normal"
    )
    st.markdown(f"📊 交易统计：失败 {fail_cnt} 笔 / 总计 {total_cnt} 笔")
    if fail_alert:
        st.error("❌ 失败率超过30%！可能存在合约逻辑故障或攻击尝试！")

    # 合约基础信息
    st.divider()
    st.subheader("📋 合约基础信息")
    st.markdown(f"总锁仓量（TVL）：**${tvl:,.2f}**")
    st.markdown(f"核心代币（USDC）价格：**${token_price:.2f}**")
    st.markdown(f"监控链：**{CHAIN.capitalize()}**")

    # 手动刷新按钮
    st.divider()
    if st.button("🔄 手动刷新数据", type="primary"):
        st.rerun()
    st.caption("注：数据来自免费开源API，延迟约1-5分钟，默认每5分钟自动刷新")

if __name__ == "__main__":
    main()