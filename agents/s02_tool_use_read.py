#!/usr/bin/env python3  # shebang行，指定使用python3解释器执行此脚本
# Harness: tool dispatch -- expanding what the model can reach.  # 注释：说明这是扩展模型可达范围的工具调度
"""
s02_tool_use.py - Tools  # 文档字符串：文件名和简要描述

The agent loop from s01 didn't change. We just added tools to the array  # s01中的代理循环没有改变，只是增加了工具数组
and a dispatch map to route calls.  # 和一个路由调用的调度映射

    +----------+      +-------+      +------------------+  # ASCII图示：用户->LLM->工具调度->具体工具执行->结果反馈的循环
    |   User   | ---> |  LLM  | ---> | Tool Dispatch    |
    |  prompt  |      |       |      | {                |
    +----------+      +---+---+      |   bash: run_bash |
                          ^          |   read: run_read |
                          |          |   write: run_wr  |
                          +----------+   edit: run_edit |
                          tool_result| }                |
                                     +------------------+

Key insight: "The loop didn't change at all. I just added tools."  # 关键洞察：循环完全没有改变，只是增加了工具
"""

import os  # 导入操作系统接口模块
import subprocess  # 导入子进程管理模块
from pathlib import Path  # 从pathlib导入Path类，用于路径操作

from anthropic import Anthropic  # 从anthropic库导入Anthropic类
from dotenv import load_dotenv  # 从dotenv库导入load_dotenv函数

load_dotenv(override=True)  # 加载环境变量，override=True表示覆盖已存在的变量

if os.getenv("ANTHROPIC_BASE_URL"):  # 如果设置了ANTHROPIC_BASE_URL环境变量
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)  # 则移除ANTHROPIC_AUTH_TOKEN环境变量

WORKDIR = Path.cwd()  # 获取当前工作目录并转换为Path对象
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # 创建Anthropic客户端实例，使用自定义base_url
MODEL = os.environ["MODEL_ID"]  # 从环境变量获取模型ID

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain."  # 系统提示：告诉AI是编码代理，使用工具解决问题，直接行动不解释


def safe_path(p: str) -> Path:  # 定义安全路径函数，确保路径不会逃逸出工作目录
    path = (WORKDIR / p).resolve()  # 将相对路径与当前工作目录组合并解析为绝对路径
    if not path.is_relative_to(WORKDIR):  # 如果解析后的路径不在工作目录内
        raise ValueError(f"Path escapes workspace: {p}")  # 抛出值错误，路径逃逸工作区
    return path  # 返回安全的Path对象


def run_bash(command: str) -> str:  # 定义运行bash命令的函数，接受字符串命令，返回字符串结果
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]  # 定义危险命令列表
    if any(d in command for d in dangerous):  # 如果命令包含任何危险指令
        return "Error: Dangerous command blocked"  # 返回错误：危险命令被阻止
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,  # 使用subprocess运行命令，shell=True允许shell语法，cwd设置工作目录
                           capture_output=True, text=True, timeout=120)  # 捕获输出，文本模式，120秒超时
        out = (r.stdout + r.stderr).strip()  # 合并标准输出和错误输出并去除首尾空白
        return out[:50000] if out else "(no output)"  # 返回前50000字符，若无输出则返回"(no output)"
    except subprocess.TimeoutExpired:  # 捕获超时异常
        return "Error: Timeout (120s)"  # 返回超时错误


def run_read(path: str, limit: int = None) -> str:  # 定义读取文件函数，接受路径和可选行数限制
    try:
        text = safe_path(path).read_text()  # 使用安全路径读取文件内容
        lines = text.splitlines()  # 按行分割文本
        if limit and limit < len(lines):  # 如果指定了限制且行数超过限制
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]  # 截取前limit行并添加提示信息
        return "\n".join(lines)[:50000]  # 合并行并返回前50000字符
    except Exception as e:  # 捕获所有异常
        return f"Error: {e}"  # 返回错误信息


def run_write(path: str, content: str) -> str:  # 定义写入文件函数，接受路径和内容
    try:
        fp = safe_path(path)  # 获取安全路径
        fp.parent.mkdir(parents=True, exist_ok=True)  # 创建父目录，parents=True创建所有必要父目录，exist_ok=True忽略已存在错误
        fp.write_text(content)  # 写入文本内容
        return f"Wrote {len(content)} bytes to {path}"  # 返回成功信息和字节数
    except Exception as e:  # 捕获所有异常
        return f"Error: {e}"  # 返回错误信息


def run_edit(path: str, old_text: str, new_text: str) -> str:  # 定义编辑文件函数，接受路径、旧文本和新文本
    try:
        fp = safe_path(path)  # 获取安全路径
        content = fp.read_text()  # 读取文件内容
        if old_text not in content:  # 如果旧文本不在内容中
            return f"Error: Text not found in {path}"  # 返回错误：文本未找到
        fp.write_text(content.replace(old_text, new_text, 1))  # 替换第一次出现的旧文本为新文本并写回文件
        return f"Edited {path}"  # 返回成功信息
    except Exception as e:  # 捕获所有异常
        return f"Error: {e}"  # 返回错误信息


# -- The dispatch map: {tool_name: handler} --  # 注释：调度映射，将工具名称映射到处理函数
TOOL_HANDLERS = {  # 定义工具处理器映射字典
    "bash": lambda **kw: run_bash(kw["command"]),  # bash工具处理器，调用run_bash函数
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),  # 读取文件工具处理器，调用run_read函数，limit为可选参数
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),  # 写入文件工具处理器，调用run_write函数
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),  # 编辑文件工具处理器，调用run_edit函数
}

TOOLS = [  # 定义可用工具列表
    {"name": "bash", "description": "Run a shell command.",  # bash工具定义
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    # bash工具输入模式
    {"name": "read_file", "description": "Read file contents.",  # 读取文件工具定义
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
                      "required": ["path"]}},  # 读取文件工具输入模式
    {"name": "write_file", "description": "Write content to file.",  # 写入文件工具定义
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                      "required": ["path", "content"]}},  # 写入文件工具输入模式
    {"name": "edit_file", "description": "Replace exact text in file.",  # 编辑文件工具定义
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"},
                                                       "new_text": {"type": "string"}},
                      "required": ["path", "old_text", "new_text"]}},  # 编辑文件工具输入模式
]


def agent_loop(messages: list):  # 定义代理循环函数，接受消息列表
    while True:  # 无限循环
        response = client.messages.create(  # 调用Anthropic API创建消息
            model=MODEL, system=SYSTEM, messages=messages,  # 指定模型、系统提示和消息历史
            tools=TOOLS, max_tokens=8000,  # 指定可用工具和最大token数
        )
        messages.append({"role": "assistant", "content": response.content})  # 追加助手的回复到消息历史
        if response.stop_reason != "tool_use":  # 如果模型没有调用工具
            return  # 结束循环
        results = []  # 初始化结果列表
        for block in response.content:  # 遍历响应内容块
            if block.type == "tool_use":  # 如果是工具使用块
                handler = TOOL_HANDLERS.get(block.name)  # 从调度映射获取对应处理器
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"  # 执行处理器或返回未知工具错误
                print(f"> {block.name}:")  # 打印工具名称
                print(output[:200])  # 打印输出的前200字符
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})  # 将工具结果添加到结果列表
        messages.append({"role": "user", "content": results})  # 将工具结果作为用户消息追加到消息历史


if __name__ == "__main__":  # 如果是主程序入口
    history = []  # 初始化消息历史为空列表
    while True:  # 主交互循环
        try:
            query = input("\033[36ms02 >> \033[0m")  # 显示青色提示符并获取用户输入
        except (EOFError, KeyboardInterrupt):  # 捕获文件结束或键盘中断异常
            break  # 退出循环
        if query.strip().lower() in ("q", "exit", ""):  # 如果输入为空或"q"/"exit"
            break  # 退出循环
        history.append({"role": "user", "content": query})  # 将用户查询添加到消息历史
        agent_loop(history)  # 调用代理循环处理查询
        response_content = history[-1]["content"]  # 获取最后一条消息内容
        if isinstance(response_content, list):  # 如果内容是一个列表
            for block in response_content:  # 遍历内容块
                if hasattr(block, "text"):  # 如果块有text属性
                    print(block.text)  # 打印文本内容
        print()  # 打印空行分隔
