#!/usr/bin/env python3  # shebang行，指定使用python3解释器执行此脚本
# Harness: persistent tasks -- goals that outlive any single conversation.  # 注释：持久化任务 - 超越单个对话的目标
"""
s07_task_system.py - Tasks  # 文档字符串：文件名和简要描述

Tasks persist as JSON files in .tasks/ so they survive context compression.  # 任务以JSON文件形式持久化存储在.tasks/目录中，因此能够经受上下文压缩的影响

Each task has a dependency graph (blockedBy).  # 每个任务都有一个依赖图（被阻塞关系）

    .tasks/  # 任务存储目录结构
      task_1.json  {"id":1, "subject":"...", "status":"completed", ...}  # 任务1：已完成
      task_2.json  {"id":2, "blockedBy":[1], "status":"pending", ...}  # 任务2：等待中，被任务1阻塞
      task_3.json  {"id":3, "blockedBy":[2], ...}  # 任务3：被任务2阻塞

    Dependency resolution:  # 依赖关系解析
    +----------+     +----------+     +----------+  # 任务依赖图：任务1完成 → 任务2解除阻塞 → 任务3解除阻塞
    | task 1   | --> | task 2   | --> | task 3   |
    | complete |     | blocked  |     | blocked  |
    +----------+     +----------+     +----------+
         |                ^
         +--- completing task 1 removes it from task 2's blockedBy  # 完成任务1会将其从任务2的blockedBy列表中移除

Key insight: "State that survives compression -- because it's outside the conversation."  # 关键洞察：在压缩中幸存的状态 - 因为它在对话之外
"""

import json  # 导入JSON处理模块
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
TASKS_DIR = WORKDIR / ".tasks"  # 设置任务存储目录路径

SYSTEM = f"You are a coding agent at {WORKDIR}. Use task tools to plan and track work."  # 系统提示：告诉AI是编码代理，使用任务工具规划和跟踪工作


# -- TaskManager: CRUD with dependency graph, persisted as JSON files --  # 注释：TaskManager：带有依赖图的CRUD操作，以JSON文件形式持久化
class TaskManager:  # 定义任务管理器类
    def __init__(self, tasks_dir: Path):  # 初始化方法
        self.dir = tasks_dir  # 设置任务目录
        self.dir.mkdir(exist_ok=True)  # 创建任务目录（如果不存在）
        self._next_id = self._max_id() + 1  # 设置下一个任务ID为当前最大ID+1

    def _max_id(self) -> int:  # 获取最大任务ID的方法
        ids = [int(f.stem.split("_")[1]) for f in self.dir.glob("task_*.json")]  # 获取所有任务文件的ID
        return max(ids) if ids else 0  # 返回最大ID，如果没有任务则返回0

    def _load(self, task_id: int) -> dict:  # 加载任务的方法
        path = self.dir / f"task_{task_id}.json"  # 构建任务文件路径
        if not path.exists():  # 如果文件不存在
            raise ValueError(f"Task {task_id} not found")  # 抛出异常：任务未找到
        return json.loads(path.read_text())  # 读取并解析JSON文件

    def _save(self, task: dict):  # 保存任务的方法
        path = self.dir / f"task_{task['id']}.json"  # 构建任务文件路径
        path.write_text(json.dumps(task, indent=2, ensure_ascii=False))  # 将任务字典写入JSON文件

    def create(self, subject: str, description: str = "") -> str:  # 创建任务的方法
        task = {  # 构建任务字典
            "id": self._next_id, "subject": subject, "description": description,  # 任务ID、主题、描述
            "status": "pending", "blockedBy": [], "owner": "",  # 状态、阻塞列表、负责人
        }
        self._save(task)  # 保存任务
        self._next_id += 1  # 递增下一个任务ID
        return json.dumps(task, indent=2, ensure_ascii=False)  # 返回格式化的任务JSON

    def get(self, task_id: int) -> str:  # 获取任务的方法
        return json.dumps(self._load(task_id), indent=2, ensure_ascii=False)  # 返回格式化的任务JSON

    def update(self, task_id: int, status: str = None,  # 更新任务的方法
               add_blocked_by: list = None, remove_blocked_by: list = None) -> str:
        task = self._load(task_id)  # 加载任务
        if status:  # 如果提供了状态
            if status not in ("pending", "in_progress", "completed"):  # 验证状态有效性
                raise ValueError(f"Invalid status: {status}")  # 抛出异常：无效状态
            task["status"] = status  # 更新状态
            if status == "completed":  # 如果状态是已完成
                self._clear_dependency(task_id)  # 清除依赖关系
        if add_blocked_by:  # 如果提供了添加阻塞列表
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))  # 合并阻塞列表并去重
        if remove_blocked_by:  # 如果提供了移除阻塞列表
            task["blockedBy"] = [x for x in task["blockedBy"] if x not in remove_blocked_by]  # 从阻塞列表中移除指定ID
        self._save(task)  # 保存更新后的任务
        return json.dumps(task, indent=2, ensure_ascii=False)  # 返回格式化的任务JSON

    def _clear_dependency(self, completed_id: int):  # 清除依赖关系的方法
        """Remove completed_id from all other tasks' blockedBy lists."""  # 从所有其他任务的blockedBy列表中移除已完成的ID
        for f in self.dir.glob("task_*.json"):  # 遍历所有任务文件
            task = json.loads(f.read_text())  # 读取任务
            if completed_id in task.get("blockedBy", []):  # 如果已完成ID在阻塞列表中
                task["blockedBy"].remove(completed_id)  # 从阻塞列表中移除
                self._save(task)  # 保存更新后的任务

    def list_all(self) -> str:  # 列出所有任务的方法
        tasks = []  # 初始化任务列表
        files = sorted(  # 获取所有任务文件并排序
            self.dir.glob("task_*.json"),
            key=lambda f: int(f.stem.split("_")[1])  # 按任务ID排序
        )
        for f in files:  # 遍历所有任务文件
            tasks.append(json.loads(f.read_text()))  # 读取并添加任务到列表
        if not tasks:  # 如果没有任务
            return "No tasks."  # 返回无任务
        lines = []  # 初始化输出行列表
        for t in tasks:  # 遍历所有任务
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")  # 根据状态选择标记符号
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""  # 构建阻塞信息字符串
            lines.append(f"{marker} #{t['id']}: {t['subject']}{blocked}")  # 格式化任务行并添加
        return "\n".join(lines)  # 返回合并后的字符串


TASKS = TaskManager(TASKS_DIR)  # 创建全局任务管理器实例


# -- Base tool implementations --  # 注释：基础工具实现
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
    "bash":        lambda **kw: run_bash(kw["command"]),  # bash命令处理器
    "read_file":   lambda **kw: run_read(kw["path"], kw.get("limit")),  # 读取文件处理器
    "write_file":  lambda **kw: run_write(kw["path"], kw["content"]),  # 写入文件处理器
    "edit_file":   lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),  # 编辑文件处理器
    "task_create": lambda **kw: TASKS.create(kw["subject"], kw.get("description", "")),  # 创建任务处理器
    "task_update": lambda **kw: TASKS.update(kw["task_id"], kw.get("status"), kw.get("addBlockedBy"), kw.get("removeBlockedBy")),  # 更新任务处理器
    "task_list":   lambda **kw: TASKS.list_all(),  # 列出所有任务处理器
    "task_get":    lambda **kw: TASKS.get(kw["task_id"]),  # 获取任务处理器
}

TOOLS = [  # 定义可用工具列表
    {"name": "bash", "description": "Run a shell command.",  # bash工具定义
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",  # 读取文件工具定义
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",  # 写入文件工具定义
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",  # 编辑文件工具定义
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "task_create", "description": "Create a new task.",  # 创建任务工具定义
     "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"]}},
    {"name": "task_update", "description": "Update a task's status or dependencies.",  # 更新任务工具定义
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "addBlockedBy": {"type": "array", "items": {"type": "integer"}}, "removeBlockedBy": {"type": "array", "items": {"type": "integer"}}}, "required": ["task_id"]}},
    {"name": "task_list", "description": "List all tasks with status summary.",  # 列出所有任务工具定义
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "task_get", "description": "Get full details of a task by ID.",  # 获取任务工具定义
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
]


def agent_loop(messages: list):  # 代理循环函数
    while True:  # 无限循环
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
            query = input("\033[36ms07 >> \033[0m")  # 显示青色提示符并获取用户输入
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
