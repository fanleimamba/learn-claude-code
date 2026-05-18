#!/usr/bin/env python3  # shebang行，指定使用python3解释器执行此脚本
# Harness: background execution -- the model thinks while the harness waits.  # 注释：后台执行 - 模型思考时harness等待
"""
s08_background_tasks.py - Background Tasks  # 文档字符串：文件名和简要描述

Run commands in background threads. A notification queue is drained  # 在后台线程中运行命令。在每次LLM调用前清空通知队列以传递结果

    Main thread                Background thread  # 主线程与后台线程的交互
    +-----------------+        +-----------------+
    | agent loop      |        | task executes   |  # 代理循环与任务执行
    | ...             |        | ...             |
    | [LLM call] <---+------- | enqueue(result) |  # LLM调用前从通知队列获取结果
    |  ^drain queue   |        +-----------------+
    +-----------------+

    Timeline:  # 时间线示例
    Agent ----[spawn A]----[spawn B]----[other work]----  # 代理生成任务A和B
                 |              |                           #
                 v              v                           # 并行执行
              [A runs]      [B runs]        (parallel)     #
                 |              |                           #
                 +-- notification queue --> [results injected]  # 结果通过通知队列注入

Key insight: "Fire and forget -- the agent doesn't block while the command runs."  # 关键洞察：即发即忘 - 代理在命令运行时不阻塞
"""

import os  # 导入操作系统接口模块
import subprocess  # 导入子进程管理模块
import threading  # 导入线程管理模块
import uuid  # 导入UUID生成模块
from pathlib import Path  # 从pathlib导入Path类，用于路径操作

from anthropic import Anthropic  # 从anthropic库导入Anthropic类
from dotenv import load_dotenv  # 从dotenv库导入load_dotenv函数

load_dotenv(override=True)  # 加载环境变量，override=True表示覆盖已存在的变量

if os.getenv("ANTHROPIC_BASE_URL"):  # 如果设置了ANTHROPIC_BASE_URL环境变量
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)  # 则移除ANTHROPIC_AUTH_TOKEN环境变量

WORKDIR = Path.cwd()  # 设置工作目录为当前工作目录
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # 创建Anthropic客户端实例，使用自定义base_url
MODEL = os.environ["MODEL_ID"]  # 从环境变量获取模型ID

SYSTEM = f"You are a coding agent at {WORKDIR}. Use background_run for long-running commands."  # 系统提示：告诉AI是编码代理，对长时间运行的命令使用background_run


# -- BackgroundManager: threaded execution + notification queue --  # 注释：BackgroundManager：线程执行 + 通知队列
class BackgroundManager:  # 定义后台管理器类
    def __init__(self):  # 初始化方法
        self.tasks = {}  # 任务字典：task_id -> {status, result, command}
        self._notification_queue = []  # 通知队列：存储完成的任务结果
        self._lock = threading.Lock()  # 线程锁：确保线程安全

    def run(self, command: str) -> str:  # 运行后台任务的方法
        """Start a background thread, return task_id immediately."""  # 启动后台线程，立即返回task_id
        task_id = str(uuid.uuid4())[:8]  # 生成8位UUID作为任务ID
        self.tasks[task_id] = {"status": "running", "result": None, "command": command}  # 初始化任务状态
        thread = threading.Thread(  # 创建新线程
            target=self._execute, args=(task_id, command), daemon=True  # 设置目标函数和参数，设为守护线程
        )
        thread.start()  # 启动线程
        return f"Background task {task_id} started: {command[:80]}"  # 返回任务启动信息

    def _execute(self, task_id: str, command: str):  # 线程执行目标函数
        """Thread target: run subprocess, capture output, push to queue."""  # 线程目标：运行子进程，捕获输出，推送到队列
        try:
            r = subprocess.run(  # 运行子进程
                command, shell=True, cwd=WORKDIR,  # 使用shell执行，设置工作目录
                capture_output=True, text=True, timeout=300  # 捕获输出，文本模式，5分钟超时
            )
            output = (r.stdout + r.stderr).strip()[:50000]  # 合并输出并截取前50000字符
            status = "completed"  # 设置状态为已完成
        except subprocess.TimeoutExpired:  # 捕获超时异常
            output = "Error: Timeout (300s)"  # 超时错误信息
            status = "timeout"  # 设置状态为超时
        except Exception as e:  # 捕获其他异常
            output = f"Error: {e}"  # 错误信息
            status = "error"  # 设置状态为错误
        self.tasks[task_id]["status"] = status  # 更新任务状态
        self.tasks[task_id]["result"] = output or "(no output)"  # 更新任务结果
        with self._lock:  # 使用线程锁
            self._notification_queue.append({  # 添加到通知队列
                "task_id": task_id,
                "status": status,
                "command": command[:80],  # 截取命令前80字符
                "result": (output or "(no output)")[:500],  # 截取结果前500字符
            })

    def check(self, task_id: str = None) -> str:  # 检查任务状态的方法
        """Check status of one task or list all."""  # 检查单个任务状态或列出所有任务
        if task_id:  # 如果提供了任务ID
            t = self.tasks.get(task_id)  # 获取任务
            if not t:  # 如果任务不存在
                return f"Error: Unknown task {task_id}"  # 返回错误信息
            return f"[{t['status']}] {t['command'][:60]}\n{t.get('result') or '(running)'}"  # 返回任务状态和结果
        lines = []  # 初始化输出行列表
        for tid, t in self.tasks.items():  # 遍历所有任务
            lines.append(f"{tid}: [{t['status']}] {t['command'][:60]}")  # 格式化任务行
        return "\n".join(lines) if lines else "No background tasks."  # 返回合并后的字符串或默认信息

    def drain_notifications(self) -> list:  # 清空通知队列的方法
        """Return and clear all pending completion notifications."""  # 返回并清除所有待处理完成通知
        with self._lock:  # 使用线程锁
            notifs = list(self._notification_queue)  # 复制通知队列
            self._notification_queue.clear()  # 清空通知队列
        return notifs  # 返回通知列表


BG = BackgroundManager()  # 创建全局后台管理器实例


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
        c = fp.read_text()  # 读取文件内容
        if old_text not in c:  # 如果要替换的文本不存在
            return f"Error: Text not found in {path}"  # 返回错误：文本未找到
        fp.write_text(c.replace(old_text, new_text, 1))  # 替换文本并写入
        return f"Edited {path}"  # 返回编辑成功信息
    except Exception as e:  # 捕获异常
        return f"Error: {e}"  # 返回错误信息


TOOL_HANDLERS = {  # 工具处理器映射
    "bash": lambda **kw: run_bash(kw["command"]),  # bash命令处理器
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),  # 读取文件处理器
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),  # 写入文件处理器
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),  # 编辑文件处理器
    "background_run": lambda **kw: BG.run(kw["command"]),  # 后台运行处理器
    "check_background": lambda **kw: BG.check(kw.get("task_id")),  # 检查后台任务处理器
}

TOOLS = [  # 定义可用工具列表
    {"name": "bash", "description": "Run a shell command (blocking).",  # bash工具定义（阻塞）
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
    {"name": "background_run", "description": "Run command in background thread. Returns task_id immediately.",
     # 后台运行工具定义
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "check_background", "description": "Check background task status. Omit task_id to list all.",  # 检查后台任务工具定义
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}}},
]


def agent_loop(messages: list):  # 代理循环函数
    while True:  # 无限循环
        # Drain background notifications and inject as system message before LLM call  # 在LLM调用前清空后台通知并作为系统消息注入
        notifs = BG.drain_notifications()  # 获取并清空后台通知
        if notifs and messages:  # 如果有通知且消息列表不为空
            notif_text = "\n".join(  # 构建通知文本
                f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs  # 格式化每个通知
            )
            messages.append({"role": "user",
                             "content": f"<background-results>\n{notif_text}\n</background-results>"})  # 将通知作为用户消息追加
        response = client.messages.create(  # 调用LLM创建消息
            model=MODEL, system=SYSTEM, messages=messages,  # 指定模型、系统提示和消息历史
            tools=TOOLS, max_tokens=8000,  # 指定可用工具和最大token数
        )
        messages.append({"role": "assistant", "content": response.content})  # 追加助手的回复到消息历史
        if response.stop_reason != "tool_use":  # 如果模型没有调用工具
            return  # 结束循环
        results = []  # 初始化结果列表
        for block in response.content:  # 遍历响应内容块
            if block.type == "tool_use":  # 如果是工具使用块
                handler = TOOL_HANDLERS.get(block.name)  # 获取工具处理器
                try:
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"  # 执行工具并获取输出
                except Exception as e:  # 捕获异常
                    output = f"Error: {e}"  # 返回错误信息
                print(f"> {block.name}:")  # 打印工具名称
                print(str(output)[:200])  # 打印输出的前200字符
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})  # 将工具结果添加到结果列表
        messages.append({"role": "user", "content": results})  # 将结果作为用户消息追加到消息历史


if __name__ == "__main__":  # 如果是主程序入口
    history = []  # 初始化消息历史为空列表
    while True:  # 主交互循环
        try:
            query = input("\033[36ms08 >> \033[0m")  # 显示青色提示符并获取用户输入
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
