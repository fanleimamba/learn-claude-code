#!/usr/bin/env python3  # shebang行，指定使用python3解释器执行此脚本
# Harness: compression -- clean memory for infinite sessions.  # 注释：压缩 - 为无限会话清理内存
"""
s06_context_compact.py - Compact  # 文档字符串：文件名和简要描述

Three-layer compression pipeline so the agent can work forever:  # 三层压缩管道，让代理可以永远工作

    Every turn:  # 每一轮
    +------------------+  # 工具调用结果
    | Tool call result |  #
    +------------------+  #
            |  # 处理流程
            v  #
    [Layer 1: micro_compact]        (silent, every turn)  # 第一层：微观压缩（静默，每轮执行）
      Replace non-read_file tool_result content older than last 3  # 替换超过最后3个的非read_file工具结果内容为占位符
      with "[Previous: used {tool_name}]"  #
            |  #
            v  #
    [Check: tokens > 50000?]  # 检查：token数是否超过50000？
       |               |  #
       no              yes  #
       |               |  #
       v               v  #
    continue    [Layer 2: auto_compact]  # 继续  第二层：自动压缩
                  Save full transcript to .transcripts/  # 保存完整对话记录到.transcripts/
                  Ask LLM to summarize conversation.  # 请求LLM总结对话
                  Replace all messages with [summary].  # 用[摘要]替换所有消息
                        |  #
                        v  #
                [Layer 3: compact tool]  # 第三层：压缩工具
                  Model calls compact -> immediate summarization.  # 模型调用压缩工具 -> 立即总结
                  Same as auto, triggered manually.  # 与自动相同，手动触发

Key insight: "The agent can forget strategically and keep working forever."  # 关键洞察：代理可以有策略地遗忘并持续工作
"""

import json  # 导入JSON处理模块
import os  # 导入操作系统接口模块
import subprocess  # 导入子进程管理模块
import time  # 导入时间模块
from pathlib import Path  # 从pathlib导入Path类，用于路径操作

from anthropic import Anthropic  # 从anthropic库导入Anthropic类
from dotenv import load_dotenv  # 从dotenv库导入load_dotenv函数

load_dotenv(override=True)  # 加载环境变量，override=True表示覆盖已存在的变量

if os.getenv("ANTHROPIC_BASE_URL"):  # 如果设置了ANTHROPIC_BASE_URL环境变量
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)  # 则移除ANTHROPIC_AUTH_TOKEN环境变量

WORKDIR = Path.cwd()  # 设置工作目录为当前工作目录
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # 创建Anthropic客户端实例，使用自定义base_url
MODEL = os.environ["MODEL_ID"]  # 从环境变量获取模型ID

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks."  # 系统提示：告诉AI是编码代理，使用工具解决问题

THRESHOLD = 50000  # 设置token阈值，超过此值触发自动压缩
TRANSCRIPT_DIR = WORKDIR / ".transcripts"  # 设置对话记录保存目录
KEEP_RECENT = 3  # 设置保留最近工具结果的数量
PRESERVE_RESULT_TOOLS = {"read_file"}  # 设置需要保留结果的工具列表，避免重复读取文件


def estimate_tokens(messages: list) -> int:  # 估算token数量的函数
    """Rough token count: ~4 chars per token."""  # 粗略估算：约4个字符一个token
    return len(str(messages)) // 4  # 返回消息总字符数除以4


# -- Layer 1: micro_compact - replace old tool results with placeholders --  # 注释：第一层：微观压缩 - 用占位符替换旧工具结果
def micro_compact(messages: list) -> list:  # 微观压缩函数，接受消息列表，返回压缩后的消息列表
    # Collect (msg_index, part_index, tool_result_dict) for all tool_result entries  # 收集所有工具结果条目的索引和字典
    tool_results = []  # 初始化工具结果列表
    for msg_idx, msg in enumerate(messages):  # 遍历所有消息
        if msg["role"] == "user" and isinstance(msg.get("content"), list):  # 如果是用户消息且内容为列表
            for part_idx, part in enumerate(msg["content"]):  # 遍历内容各部分
                if isinstance(part, dict) and part.get("type") == "tool_result":  # 如果是工具结果类型
                    tool_results.append((msg_idx, part_idx, part))  # 添加到工具结果列表
    if len(tool_results) <= KEEP_RECENT:  # 如果工具结果数量不超过保留数量
        return messages  # 直接返回原始消息
    # Find tool_name for each result by matching tool_use_id in prior assistant messages  # 通过匹配之前的助手消息中的tool_use_id查找工具名称
    tool_name_map = {}  # 初始化工具名称映射字典
    for msg in messages:  # 遍历所有消息
        if msg["role"] == "assistant":  # 如果是助手消息
            content = msg.get("content", [])  # 获取内容
            if isinstance(content, list):  # 如果内容是列表
                for block in content:  # 遍历内容块
                    if hasattr(block, "type") and block.type == "tool_use":  # 如果是工具使用类型
                        tool_name_map[block.id] = block.name  # 添加到工具名称映射
    # Clear old results (keep last KEEP_RECENT). Preserve read_file outputs because  # 清除旧结果（保留最后KEEP_RECENT个）。保留read_file输出因为
    # they are reference material; compacting them forces the agent to re-read files.  # 它们是参考资料；压缩它们会迫使代理重新读取文件
    to_clear = tool_results[:-KEEP_RECENT]  # 获取需要清除的旧结果
    for _, _, result in to_clear:  # 遍历需要清除的结果
        if not isinstance(result.get("content"), str) or len(result["content"]) <= 100:  # 如果内容不是字符串或长度不超过100
            continue  # 跳过
        tool_id = result.get("tool_use_id", "")  # 获取工具使用ID
        tool_name = tool_name_map.get(tool_id, "unknown")  # 获取工具名称
        if tool_name in PRESERVE_RESULT_TOOLS:  # 如果工具在保留列表中
            continue  # 跳过
        result["content"] = f"[Previous: used {tool_name}]"  # 替换内容为占位符
    return messages  # 返回压缩后的消息


# -- Layer 2: auto_compact - save transcript, summarize, replace messages --  # 注释：第二层：自动压缩 - 保存对话记录、总结、替换消息
def auto_compact(messages: list) -> list:  # 自动压缩函数，接受消息列表，返回压缩后的消息列表
    # Save full transcript to disk  # 保存完整对话记录到磁盘
    TRANSCRIPT_DIR.mkdir(exist_ok=True)  # 创建对话记录目录
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"  # 创建对话记录文件路径
    with open(transcript_path, "w") as f:  # 打开文件写入
        for msg in messages:  # 遍历所有消息
            f.write(json.dumps(msg, default=str) + "\n")  # 写入JSON格式的消息
    print(f"[transcript saved: {transcript_path}]")  # 打印保存的对话记录路径
    # Ask LLM to summarize  # 请求LLM总结
    conversation_text = json.dumps(messages, default=str)[-80000:]  # 获取对话文本的最后80000字符
    response = client.messages.create(  # 调用LLM创建消息
        model=MODEL,  # 指定模型
        messages=[{"role": "user", "content":  # 创建总结请求消息
            "Summarize this conversation for continuity. Include: "  # 总结此对话以保持连续性，包括：
            "1) What was accomplished, 2) Current state, 3) Key decisions made. "  # 1)完成了什么，2)当前状态，3)做出的关键决策
            "Be concise but preserve critical details.\n\n" + conversation_text}],  # 简洁但保留关键细节
        max_tokens=2000,  # 最大token数
    )
    summary = next((block.text for block in response.content if hasattr(block, "text")), "")  # 提取总结文本
    if not summary:  # 如果没有总结
        summary = "No summary generated."  # 默认总结
    # Replace all messages with compressed summary  # 用压缩后的总结替换所有消息
    return [  # 返回只包含总结的新消息列表
        {"role": "user", "content": f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}"},
        # 包含对话记录路径和总结
    ]


# -- Tool implementations --  # 注释：工具实现
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
    "compact": lambda **kw: "Manual compression requested.",  # 压缩工具处理器
}

TOOLS = [  # 定义可用工具列表
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
    {"name": "compact", "description": "Trigger manual conversation compression.",  # 压缩工具定义
     "input_schema": {"type": "object",
                      "properties": {"focus": {"type": "string", "description": "What to preserve in the summary"}}}},
    # 可选参数：在总结中保留什么
]


def agent_loop(messages: list):  # 代理循环函数
    while True:  # 无限循环
        # Layer 1: micro_compact before each LLM call  # 第一层：每次调用LLM前进行微观压缩
        micro_compact(messages)  # 执行微观压缩
        # Layer 2: auto_compact if token estimate exceeds threshold  # 第二层：如果token估算超过阈值则进行自动压缩
        if estimate_tokens(messages) > THRESHOLD:  # 如果token估算超过阈值
            print("[auto_compact triggered]")  # 打印自动压缩触发信息
            messages[:] = auto_compact(messages)  # 执行自动压缩并替换消息列表
        response = client.messages.create(  # 调用LLM创建消息
            model=MODEL, system=SYSTEM, messages=messages,  # 指定模型、系统提示和消息历史
            tools=TOOLS, max_tokens=8000,  # 指定可用工具和最大token数
        )
        messages.append({"role": "assistant", "content": response.content})  # 追加助手的回复到消息历史
        if response.stop_reason != "tool_use":  # 如果模型没有调用工具
            return  # 结束循环
        results = []  # 初始化结果列表
        manual_compact = False  # 初始化手动压缩标志
        for block in response.content:  # 遍历响应内容块
            if block.type == "tool_use":  # 如果是工具使用块
                if block.name == "compact":  # 如果是压缩工具
                    manual_compact = True  # 设置手动压缩标志
                    output = "Compressing..."  # 输出压缩中信息
                else:
                    handler = TOOL_HANDLERS.get(block.name)  # 获取工具处理器
                    try:
                        output = handler(**block.input) if handler else f"Unknown tool: {block.name}"  # 执行工具并获取输出
                    except Exception as e:  # 捕获异常
                        output = f"Error: {e}"  # 返回错误信息
                print(f"> {block.name}:")  # 打印工具名称
                print(str(output)[:200])  # 打印输出的前200字符
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})  # 将工具结果添加到结果列表
        messages.append({"role": "user", "content": results})  # 将结果作为用户消息追加到消息历史
        # Layer 3: manual compact triggered by the compact tool  # 第三层：由压缩工具触发的手动压缩
        if manual_compact:  # 如果触发了手动压缩
            print("[manual compact]")  # 打印手动压缩信息
            messages[:] = auto_compact(messages)  # 执行自动压缩并替换消息列表
            return  # 结束循环


if __name__ == "__main__":  # 如果是主程序入口
    history = []  # 初始化消息历史为空列表
    while True:  # 主交互循环
        try:
            query = input("\033[36ms06 >> \033[0m")  # 显示青色提示符并获取用户输入
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
