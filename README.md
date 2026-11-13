# termfolio

字符终端的屏幕投影工具及可交互的本地台账程序。

## 现有能力

inspect 查看一份源文件的结构；capture 发起一次本机 HTTP 请求并保存响应状态、原始字节和摘要；show 列出接收记录。原文对象按 SHA-256 共用，独立请求分别记账。错误 HTTP 状态也会保存，原文归档不等于业务接收完成。collect 通过 PTY 真正驱动本地终端程序采集台账，replay 把采集保存的原始字节流重新投影成整屏。

## 运行

使用 Python 3.11 或更新版本，仅依赖标准库，无安装步骤。全部示例均为本地合成资料，不包含真实个人信息、商业凭据或线上地址。

```sh
python3 -m termfolio inspect termfolio/fixtures/screen.ansi
python3 -m termfolio demo
python3 -m termfolio collect --run-dir runs/nightly --program "python3 -m termfolio.ledger --update-after-details 1"
python3 -m termfolio replay runs/nightly/raw/attempt-1.ansi
python3 -m unittest discover -s tests -v
python3 -m compileall -q termfolio
```

需要手动发请求时运行 `python3 -m termfolio.source_server --port 8765`，再在另一终端运行 `python3 -m termfolio capture http://127.0.0.1:8765/screen`。默认会在当前目录创建 captures.sqlite3 和 objects；可通过 --db 和 --objects 指定独立位置。演示自己启动源服务，使用随机本机端口，并在退出时清理临时目录。

## PTY 台账采集（collect）

采集器只通过伪终端与台账程序收发按键和终端输出，不读取台账程序的数据文件，也不要求它增加数据接口。

* **按键白名单**：只允许 n/p（翻页）、j/k（定位）、o（详情）、b（返回）、空格（确认屏幕提示）、q（退出）。m/s 等写入或发运入口在驱动层即被拒绝。
* **按屏幕决定动作**：每次按键后等待整屏在屏蔽时钟行后连续静止，再解释刷新完成的屏幕（版本、页号、焦点、模态、详情字段）。没有固定 sleep，也不在原始输出流里搜索关键字；时钟的持续局部刷新不参与静止判断。
* **版本边界**：一份导出只来自同一台账版本。遇到“台账已更新”模态，确认后旧版本已抄行全部作废并在日志留痕，新版本从首页重新采集；重入时先核对版本，版本不符立即失败。
* **重叠与去重**：相邻页重叠的行按完整箱号去重，每次出现都保留屏幕快照与来源关联；同箱号读到不一致内容直接停止，避免抄错。
* **详情语义**：详情屏读不到完整箱号属于详情失败并停止；备注字段存在但内容为空属于本来为空，正常导出为空字符串，二者明确区分。
* **续采**：`--pause-after-page N` 可在完成第 N 页后主动 q 退出；对同一 `--run-dir` 再次执行 collect 会重开程序、核对版本并逐页核对定位，再从下一个未完成页继续。失败时 state.json 与标准错误输出运营可见的完整屏幕和停止原因，失败的运行不会被静默续采。
* **证据**：运行目录下 `raw/` 保存每次 PTY 会话的原始字节流，`screens/` 保存刷新后整屏文本，`keys/` 保存按键日志，`journal.jsonl` 追加记录全部事件，`state.json` 原子更新断点状态，`export.json` 为最终导出（每条箱号带 sources 来源关联）。replay 与 inspect 均可对 raw 流重建屏幕。

## 模块

`termfolio/parser.py` 负责单文档观察，`archive.py` 保存不可变原文与请求事实，`transport.py` 限制实验请求到本机，`source_server.py` 提供来源示例，`terminal.py` 提供 PTY 驱动与整屏状态解释，`collector.py` 实现采集状态机、版本守卫与证据落盘。来源边界及字段含义见 `docs/source-contract.md`。这些入口不会声称一次响应已经构成完整目录或业务批次。

## 容器

Dockerfile 使用 Python 3.11 标准库镜像，不需要应用依赖下载。可用 `docker build -t termfolio:local .` 构建，再执行 `docker run --rm --network none termfolio:local`。本地 HTTP 与 PTY 演练不需要外部网络，测试及单次演示通常数秒完成。
