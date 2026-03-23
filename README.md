# 美股 250 日线突破筛选器

这个首版工具会筛选：

- 今天收盘价刚刚向上突破 250 日均线的美股
- 或者最近 5 个交易日内首次向上突破 250 日均线，且最新收盘价仍站在 250 日均线上方的美股

当前实现默认覆盖官方 Nasdaq symbol directory 里的美股上市股票池：

- `nasdaqlisted.txt`
- `otherlisted.txt`

默认排除：

- ETF
- test issues
- 明显不是普通股票的证券类型，例如 warrant、right、unit、preferred、fund、trust、note 等

## 安装

```bash
python3 -m pip install -r requirements.txt
```

## 用法

筛全部美股，找最近一周内刚刚突破 250 日线的股票：

```bash
python3 stock_screener.py --lookback-days 5 --output-csv output/breakouts.csv
```

只筛今天刚突破的股票：

```bash
python3 stock_screener.py --lookback-days 1
```

先做小范围验证：

```bash
python3 stock_screener.py --symbols AAPL,MSFT,NVDA,TSLA,BRK.B
```

更严格一点，只保留最新收盘价距离 250 日线不超过 3% 的股票：

```bash
python3 stock_screener.py --lookback-days 5 --max-distance-pct 3
```

如果你想只保留更严格意义上的普通股/普通股等价证券：

```bash
python3 stock_screener.py --lookback-days 3 --strict-common-stock
```

如果你想切回轻量但更容易限流的备用源：

```bash
python3 stock_screener.py --provider stooq --max-symbols 200
```

快速 smoke run：

```bash
python3 stock_screener.py --max-symbols 200
```

## 输出字段

- `symbol`: 股票代码
- `exchange`: 交易所
- `breakout_date`: 首次突破 250 日线的交易日
- `sessions_since_breakout`: 距离突破日过去了几个交易日
- `latest_close`: 最新收盘价
- `latest_ma250`: 最新 250 日均线
- `latest_premium_pct`: 最新收盘价相对 250 日线的偏离百分比

## 判定逻辑

工具把“刚刚突破 250 天线”定义为：

1. 某个交易日收盘价 `Close[t] > MA250[t]`
2. 前一个交易日收盘价 `Close[t-1] <= MA250[t-1]`
3. 这个突破发生在最近 `N` 个交易日内，默认 `N=5`
4. 最新收盘价仍然在最新的 250 日均线之上

## 缓存

- 股票池和行情数据默认缓存到 `.cache/`
- 默认 20 小时内复用缓存
- 加 `--refresh` 可以强制刷新
- 不同数据源会分别缓存，互不污染

## 测试

```bash
python3 -m unittest discover -s tests
```

## 本地归档查看软件

启动本地软件：

```bash
python3 stock_tracker_app.py
```

然后在浏览器打开：

```text
http://127.0.0.1:8765
```

这个软件可以：

- 自动读取 `output/` 目录里的扫描结果 CSV，当作归档快照
- 左边浏览股票清单，支持上下键切换
- 右边联动 K 线，并保留一键打开 TradingView 的入口
- 给每只股票写备注、标记状态，并自动保存到 SQLite 数据库 `data/stock_tracker.db`
- 查看同一只股票在历史扫描结果里的变化记录

## 公网部署准备

服务端已经支持下面这些部署参数：

- `HOST`: 监听地址，公网部署时一般用 `0.0.0.0`
- `PORT`: 监听端口，默认 `8765`
- `STOCK_TRACKER_DATA_DIR`: 备注数据库目录
- `STOCK_TRACKER_DB_PATH`: SQLite 数据库文件路径
- `STOCK_TRACKER_OUTPUT_DIR`: 扫描结果目录
- `STOCK_TRACKER_CACHE_DIR`: K 线缓存目录
- `STOCK_TRACKER_BASIC_AUTH_USER`: 共享访问用户名
- `STOCK_TRACKER_BASIC_AUTH_PASSWORD`: 共享访问密码

仓库里已经附带：

- [render.yaml](/Users/jamesma/Documents/股票工具/render.yaml): Render 蓝图配置，包含持久化磁盘
- [railway.json](/Users/jamesma/Documents/股票工具/railway.json): Railway 配置，走 Dockerfile 部署
- [Dockerfile](/Users/jamesma/Documents/股票工具/Dockerfile): 两个平台都可直接使用

本地数据库、缓存和临时文件已经通过 [/.gitignore](/Users/jamesma/Documents/股票工具/.gitignore) 排除，不会被推到 GitHub。

Docker 方式启动：

```bash
docker build -t stock-tracker .
docker run -d \
  -p 8080:8080 \
  -v "$(pwd)/data:/data" \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/.cache:/app/.cache" \
  --name stock-tracker \
  stock-tracker
```

## GitHub + Render 部署

这套项目更推荐部署到 Render，因为它支持在 Web Service 上直接挂持久化磁盘，适合保存你爸写下来的备注和状态。

1. 把仓库推到 GitHub
2. 在 Render 里选择 `New +` -> `Blueprint`
3. 连接 GitHub 仓库，选择这个项目
4. Render 会自动读取 `render.yaml`
5. 首次部署时填入可选的共享账号密码：
   - `STOCK_TRACKER_BASIC_AUTH_USER`
   - `STOCK_TRACKER_BASIC_AUTH_PASSWORD`
6. 部署完成后，备注数据库会保存到挂载的 `/data/stock_tracker.db`

默认归档股票清单读取仓库中的 `output/` 目录，所以你只要把新的扫描 CSV 推到 GitHub 并重新部署，网站就能显示新的批次。

## GitHub + Railway 部署

如果你更习惯 Railway，也可以直接部署：

1. 在 Railway 新建项目并连接 GitHub 仓库
2. Railway 会读取 `Dockerfile`，并可参考 `railway.json`
3. 在 Railway 里添加一个持久化 Volume，并挂载到 `/data`
4. 添加环境变量：
   - `HOST=0.0.0.0`
   - `STOCK_TRACKER_DATA_DIR=/data`
   - `STOCK_TRACKER_DB_PATH=/data/stock_tracker.db`
   - `STOCK_TRACKER_OUTPUT_DIR=/app/output`
   - `STOCK_TRACKER_CACHE_DIR=/tmp/stock-cache`
5. 如果需要共享登录，再额外设置：
   - `STOCK_TRACKER_BASIC_AUTH_USER`
   - `STOCK_TRACKER_BASIC_AUTH_PASSWORD`

Railway 也能满足需求，但这套项目里我更建议优先用 Render，配置更直接一点。

## 当前假设

- 默认行情源使用 `yfinance` 批量日线下载
- `stooq` 保留为备用源，适合小批量快速筛选
- 右侧图表默认叠加两条简单移动平均线：`SMA(120)` 与 `SMA(250)`，并保留一键打开 TradingView 的入口
- 最近一周按最近 5 个交易日计算
- “所有美股”当前按官方上市股票池近似处理，不包含 OTC
- `--strict-common-stock` 会额外排除 ADR、基金、证书类证券
