# termfolio

字符终端的屏幕投影工具及可交互的本地台账程序。

## 现有能力

inspect 查看一份源文件的结构；capture 发起一次本机 HTTP 请求并保存响应状态、原始字节和摘要；show 列出接收记录。原文对象按 SHA-256 共用，独立请求分别记账。错误 HTTP 状态也会保存，原文归档不等于业务接收完成。

## 运行

使用 Python 3.11 或更新版本，仅依赖标准库，无安装步骤。全部示例均为本地合成资料，不包含真实个人信息、商业凭据或线上地址。

```sh
python3 -m termfolio inspect termfolio/fixtures/screen.ansi
python3 -m termfolio demo
python3 -m unittest discover -s tests -v
python3 -m compileall -q termfolio
```

需要手动发请求时运行 `python3 -m termfolio.source_server --port 8765`，再在另一终端运行 `python3 -m termfolio capture http://127.0.0.1:8765/screen`。默认会在当前目录创建 captures.sqlite3 和 objects；可通过 --db 和 --objects 指定独立位置。演示自己启动源服务，使用随机本机端口，并在退出时清理临时目录。

## 模块

`termfolio/parser.py` 负责单文档观察，`archive.py` 保存不可变原文与请求事实，`transport.py` 限制实验请求到本机，`source_server.py` 提供来源示例。来源边界及字段含义见 `docs/source-contract.md`。这些入口不会声称一次响应已经构成完整目录或业务批次。

## 容器

Dockerfile 使用 Python 3.11 标准库镜像，不需要应用依赖下载。可用 `docker build -t termfolio:local .` 构建，再执行 `docker run --rm --network none termfolio:local`。本地 HTTP 与 PTY 演练不需要外部网络，测试及单次演示通常数秒完成。
