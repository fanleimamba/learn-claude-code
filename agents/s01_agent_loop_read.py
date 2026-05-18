#!/usr/bin/env python3  # shebang行，指定使用python3解释器执行此脚本
# Harness: the loop -- the model's first connection to the real world.  # 注释：说明这是模型与现实世界首次连接的核心循环
"""
s01_agent_loop.py - The Agent Loop  # 文档字符串：文件名和简要描述

### 逐行解释这个python代码，在每一行后面备注功能，在一个输出中给出结果

The entire secret of an AI coding agent in one pattern:  # AI编码代理的全部秘密在于以下模式

    while stop_reason == "tool_use":  # 当停止原因为"tool_use"时继续循环
        response = LLM(messages, tools)  # 调用LLM处理消息和工具
        execute tools  # 执行工具
        append results  # 追加结果到消息中

    +----------+      +-------+      +---------+  # ASCII图示：用户->LLM->工具->结果反馈的循环
    |   User   | ---> |  LLM  | ---> |  Tool   |
    |  prompt  |      |       |      | execute |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                          (loop continues)

This is the core loop: feed tool results back to the model  # 核心循环：将工具结果反馈给模型
until the model decides to stop. Production agents layer  # 直到模型决定停止。生产级代理在此基础上添加策略、钩子和生命周期控制
policy, hooks, and lifecycle controls on top.
"""

import os  # 导入操作系统接口模块
import subprocess  # 导入子进程管理模块

try:
    import readline  # 尝试导入readline模块，用于命令行历史记录和编辑功能

    # #143 UTF-8 backspace fix for macOS libedit  # 针对macOS libedit的UTF-8退格键修复
    readline.parse_and_bind('set bind-tty-special-chars off')  # 关闭tty特殊字符绑定
    readline.parse_and_bind('set input-meta on')  # 启用元字符输入
    readline.parse_and_bind('set output-meta on')  # 启用元字符输出
    readline.parse_and_bind('set convert-meta off')  # 关闭元字符转换
    readline.parse_and_bind('set enable-meta-keybindings on')  # 启用元键绑定
except ImportError:
    pass  # 如果导入失败则忽略

from anthropic import Anthropic  # 从anthropic库导入Anthropic类
from dotenv import load_dotenv  # 从dotenv库导入load_dotenv函数

load_dotenv(override=True)  # 加载环境变量，override=True表示覆盖已存在的变量

if os.getenv("ANTHROPIC_BASE_URL"):  # 如果设置了ANTHROPIC_BASE_URL环境变量
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)  # 则移除ANTHROPIC_AUTH_TOKEN环境变量

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # 创建Anthropic客户端实例，使用自定义base_url
MODEL = os.environ["MODEL_ID"]  # 从环境变量获取模型ID

SYSTEM = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."  # 系统提示：告诉AI是编码代理，使用bash解决问题，直接行动不解释

TOOLS = [{  # 定义可用工具列表
    "name": "bash",  # 工具名称：bash
    "description": "Run a shell command.",  # 工具描述：运行shell命令
    "input_schema": {  # 输入模式定义
        "type": "object",  # 输入类型为对象
        "properties": {"command": {"type": "string"}},  # 属性：command为字符串类型
        "required": ["command"],  # 必需字段：command
    },
}]


def run_bash(command: str) -> str:  # 定义运行bash命令的函数，接受字符串命令，返回字符串结果
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]  # 定义危险命令列表
    if any(d in command for d in dangerous):  # 如果命令包含任何危险指令
        return "Error: Dangerous command blocked"  # 返回错误：危险命令被阻止
    try:
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),  # 使用subprocess运行命令，shell=True允许shell语法，cwd设置工作目录
                           capture_output=True, text=True, timeout=120)  # 捕获输出，文本模式，120秒超时
        out = (r.stdout + r.stderr).strip()  # 合并标准输出和错误输出并去除首尾空白
        return out[:50000] if out else "(no output)"  # 返回前50000字符，若无输出则返回"(no output)"
    except subprocess.TimeoutExpired:  # 捕获超时异常
        return "Error: Timeout (120s)"  # 返回超时错误
    except (FileNotFoundError, OSError) as e:  # 捕获文件未找到或操作系统错误
        return f"Error: {e}"  # 返回具体错误信息


# -- The core pattern: a while loop that calls tools until the model stops --  # 注释：核心模式 - 调用工具直到模型停止的while循环
def agent_loop(messages: list):  # 定义代理循环函数，接受消息列表
    while True:  # 无限循环
        response = client.messages.create(  # 调用Anthropic API创建消息
            model=MODEL, system=SYSTEM, messages=messages,  # 指定模型、系统提示和消息历史
            tools=TOOLS, max_tokens=8000,  # 指定可用工具和最大token数
        )
        # Append assistant turn  # 追加助手的回复到消息历史
        messages.append({"role": "assistant", "content": response.content})
        # If the model didn't call a tool, we're done  # 如果模型没有调用工具，则结束循环
        if response.stop_reason != "tool_use":
            return
        # Execute each tool call, collect results  # 执行每个工具调用并收集结果
        results = []
        for block in response.content:  # 遍历响应内容块
            if block.type == "tool_use":  # 如果是工具使用块
                print(f"\033[33m$ {block.input['command']}\033[0m")  # 打印黄色命令提示符和命令
                output = run_bash(block.input["command"])  # 运行bash命令获取输出
                print(output[:200])  # 打印输出的前200字符
                results.append({"type": "tool_result", "tool_use_id": block.id,  # 将工具结果添加到结果列表
                                "content": output})
        messages.append({"role": "user", "content": results})  # 将工具结果作为用户消息追加到消息历史


if __name__ == "__main__":  # 如果是主程序入口
    history = []  # 初始化消息历史为空列表
    while True:  # 主交互循环
        try:
            query = input("\033[36ms01 >> \033[0m")  # 显示青色提示符并获取用户输入
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
