#!/usr/bin/env python3  # shebang行，指定使用python3解释器执行此脚本
# Harness: context isolation -- protecting the model's clarity of thought.  # 注释：上下文隔离 - 保护模型的思维清晰度
"""
s04_subagent.py - Subagents  # 文档字符串：文件名和简要描述

Spawn a child agent with fresh messages=[]. The child works in its own  # 生成一个子代理，使用新的空消息列表。子代理在自己的上下文中工作
context, sharing the filesystem, then returns only a summary to the parent.  # 共享文件系统，但只向父代理返回摘要

    Parent agent                     Subagent  # ASCII图示：父代理和子代理的交互流程
    +------------------+             +------------------+
    | messages=[...]   |             | messages=[]      |  <-- fresh  # 父代理有历史消息，子代理从空消息开始
    |                  |  dispatch   |                  |
    | tool: task       | ---------->| while tool_use:  |  # 父代理通过task工具分派任务给子代理
    |   prompt="..."   |            |   call tools     |
    |   description="" |            |   append results |
    |                  |  summary   |                  |
    |   result = "..." | <--------- | return last text |  # 子代理返回摘要结果
    +------------------+             +------------------+
              |
    Parent context stays clean.  # 父代理上下文保持清洁
    Subagent context is discarded.  # 子代理上下文被丢弃

Key insight: "Process isolation gives context isolation for free."  # 关键洞察：进程隔离天然提供上下文隔离
"""

import os  # 导入操作系统接口模块
import subprocess  # 导入子进程管理模块
from pathlib import Path  # 从pathlib导入Path类，用于路径操作

from anthropic import Anthropic  # 从anthropic库导入Anthropic类
from dotenv import load_dotenv  # 从dotenv库导入load_dotenv函数

load_dotenv(override=True)  # 加载环境变量，override=True表示覆盖已存在的变量

if os.getenv("ANTHROPIC_BASE_URL"):  # 如果设置了ANTHROPIC_BASE_URL环境变量
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)  # 则移除ANTHROPIC_AUTH_TOKEN环境变量

WORKDIR = Path.cwd()  # 设置工作目录为当前工作目录
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # 创建Anthropic客户端实例，使用自定义base_url
MODEL = os.environ["MODEL_ID"]  # 从环境变量获取模型ID

SYSTEM = f"You are a coding agent at {WORKDIR}. Use the task tool to delegate exploration or subtasks."  # 父代理系统提示：使用task工具委派探索或子任务
SUBAGENT_SYSTEM = f"You are a coding subagent at {WORKDIR}. Complete the given task, then summarize your findings."  # 子代理系统提示：完成给定任务，然后总结发现


# -- Tool implementations shared by parent and child --  # 注释：父代理和子代理共享的工具实现
def safe_path(p: str) -> Path:  # 安全路径函数，确保路径不会逃逸工作目录
    path = (WORKDIR / p).resolve()  # 解析相对路径
    if not path.is_relative_to(WORKDIR):  # 如果路径不在工作目录内
        raise ValueError(f"Path escapes workspace: {p}")  # 抛出异常：路径逃逸工作目录
    return path  # 返回安全路径


def run_bash(command: str) -> str:  # 运行bash命令的函数
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]  # 定义危险命令列表
    if any(d in command for d in dangerous):  # 如果命令包含危险指令
        return "Error: Dangerous command blocked"  # 返回错误：危险命令被阻止
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,  # 运行命令，shell=True允许shell语法，cwd设置工作目录
                           capture_output=True, text=True, timeout=120)  # 捕获输出，文本模式，120秒超时
        out = (r.stdout + r.stderr).strip()  # 合并标准输出和错误输出并去除首尾空白
        return out[:50000] if out else "(no output)"  # 返回前50000字符，若无输出则返回"(no output)"
    except subprocess.TimeoutExpired:  # 捕获超时异常
        return "Error: Timeout (120s)"  # 返回超时错误
    except (FileNotFoundError, OSError) as e:  # 捕获文件未找到或操作系统错误
        return f"Error: {e}"  # 返回具体错误信息


def run_read(path: str, limit: int = None) -> str:  # 读取文件内容的函数
    try:
        lines = safe_path(path).read_text().splitlines()  # 读取文件内容并分割成行
        if limit and limit < len(lines):  # 如果设置了限制且行数超过限制
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]  # 截取前limit行并添加提示
        return "\n".join(lines)[:50000]  # 返回合并后的字符串，最多50000字符
    except Exception as e:  # 捕获异常
        return f"Error: {e}"  # 返回错误信息


def run_write(path: str, content: str) -> str:  # 写入文件内容的函数
    try:
        fp = safe_path(path)  # 获取安全路径
        fp.parent.mkdir(parents=True, exist_ok=True)  # 创建父目录
        fp.write_text(content)  # 写入内容
        return f"Wrote {len(content)} bytes"  # 返回写入字节数
    except Exception as e:  # 捕获异常
        return f"Error: {e}"  # 返回错误信息


def run_edit(path: str, old_text: str, new_text: str) -> str:  # 编辑文件内容的函数
    try:
        fp = safe_path(path)  # 获取安全路径
        content = fp.read_text()  # 读取文件内容
        if old_text not in content:  # 如果要替换的文本不存在
            return f"Error: Text not found in {path}"  # 返回错误：文本未找到
        fp.write_text(content.replace(old_text, new_text, 1))  # 替换文本并写入
        return f"Edited {path}"  # 返回编辑成功信息
    except Exception as e:  # 捕获异常
        return f"Error: {e}"  # 返回错误信息


TOOL_HANDLERS = {  # 工具处理器映射
    "bash": lambda **kw: run_bash(kw["command"]),  # bash命令处理器
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),  # 读取文件处理器
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),  # 写入文件处理器
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),  # 编辑文件处理器
}

# Child gets all base tools except task (no recursive spawning)  # 子代理获得所有基础工具，但不包括task工具（防止递归生成）
CHILD_TOOLS = [  # 子代理可用工具列表
    {"name": "bash", "description": "Run a shell command.",  # bash工具定义
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",  # 读取文件工具定义
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",  # 写入文件工具定义
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",  # 编辑文件工具定义
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"},
                                                       "new_text": {"type": "string"}},
                      "required": ["path", "old_text", "new_text"]}},
]


# -- Subagent: fresh context, filtered tools, summary-only return --  # 注释：子代理：新的上下文、过滤的工具、仅返回摘要
def run_subagent(prompt: str) -> str:  # 运行子代理的函数，接受提示字符串，返回摘要字符串
    sub_messages = [{"role": "user", "content": prompt}]  # 创建新的空消息列表，只包含用户提示
    for _ in range(30):  # 安全限制，最多30轮对话
        response = client.messages.create(  # 调用LLM创建消息
            model=MODEL, system=SUBAGENT_SYSTEM, messages=sub_messages,  # 使用子代理系统提示和消息历史
            tools=CHILD_TOOLS, max_tokens=8000,  # 指定子代理可用工具和最大token数
        )
        sub_messages.append({"role": "assistant", "content": response.content})  # 追加助手的回复到消息历史
        if response.stop_reason != "tool_use":  # 如果模型没有调用工具
            break  # 结束循环
        results = []  # 初始化结果列表
        for block in response.content:  # 遍历响应内容块
            if block.type == "tool_use":  # 如果是工具使用块
                handler = TOOL_HANDLERS.get(block.name)  # 获取工具处理器
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"  # 执行工具并获取输出
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": str(output)[:50000]})  # 将工具结果添加到结果列表
        sub_messages.append({"role": "user", "content": results})  # 将工具结果作为用户消息追加到消息历史
    # Only the final text returns to the parent -- child context is discarded  # 只有最终文本返回给父代理 - 子代理上下文被丢弃
    return "".join(
        b.text for b in response.content if hasattr(b, "text")) or "(no summary)"  # 返回所有文本块的合并结果，如果没有文本则返回默认摘要


# -- Parent tools: base tools + task dispatcher --  # 注释：父代理工具：基础工具 + 任务分派器
PARENT_TOOLS = CHILD_TOOLS + [  # 父代理工具是子代理工具加上task工具
    {"name": "task",
     "description": "Spawn a subagent with fresh context. It shares the filesystem but not conversation history.",
     # task工具定义
     "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}, "description": {"type": "string",
                                                                                                     "description": "Short description of the task"}},
                      "required": ["prompt"]}},
]


def agent_loop(messages: list):  # 代理循环函数
    while True:  # 无限循环
        response = client.messages.create(  # 调用LLM创建消息
            model=MODEL, system=SYSTEM, messages=messages,  # 指定模型、系统提示和消息历史
            tools=PARENT_TOOLS, max_tokens=8000,  # 指定父代理可用工具和最大token数
        )
        messages.append({"role": "assistant", "content": response.content})  # 追加助手的回复到消息历史
        if response.stop_reason != "tool_use":  # 如果模型没有调用工具
            return  # 结束循环
        results = []  # 初始化结果列表
        for block in response.content:  # 遍历响应内容块
            if block.type == "tool_use":  # 如果是工具使用块
                if block.name == "task":  # 如果是task工具
                    desc = block.input.get("description", "subtask")  # 获取任务描述，默认为"subtask"
                    prompt = block.input.get("prompt", "")  # 获取任务提示
                    print(f"> task ({desc}): {prompt[:80]}")  # 打印任务信息
                    output = run_subagent(prompt)  # 运行子代理
                else:
                    handler = TOOL_HANDLERS.get(block.name)  # 获取工具处理器
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"  # 执行工具并获取输出
                print(f"  {str(output)[:200]}")  # 打印输出的前200字符
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})  # 将工具结果添加到结果列表
        messages.append({"role": "user", "content": results})  # 将结果作为用户消息追加到消息历史


if __name__ == "__main__":  # 如果是主程序入口
    history = []  # 初始化消息历史为空列表
    while True:  # 主交互循环
        try:
            query = input("\033[36ms04 >> \033[0m")  # 显示青色提示符并获取用户输入
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
