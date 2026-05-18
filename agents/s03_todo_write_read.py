#!/usr/bin/env python3  # shebang行，指定使用python3解释器执行此脚本
# Harness: planning -- keeping the model on course without scripting the route.  # 注释：规划功能 - 让模型保持正确方向而不需要脚本化路径
"""
s03_todo_write.py - TodoWrite  # 文档字符串：文件名和简要描述

The model tracks its own progress via a TodoManager. A nag reminder  # 模型通过TodoManager跟踪自己的进度。一个提醒功能
forces it to keep updating when it forgets.  # 在模型忘记时强制它保持更新

    +----------+      +-------+      +---------+  # ASCII图示：用户->LLM->工具->结果反馈的循环，增加了todo管理
    |   User   | ---> |  LLM  | ---> | Tools   |
    |  prompt  |      |       |      | + todo  |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                                |
                    +-----------+-----------+
                    | TodoManager state     |  # TodoManager状态管理
                    | [ ] task A            |  # 待办任务A
                    | [>] task B <- doing   |  # 进行中任务B
                    | [x] task C            |  # 已完成任务C
                    +-----------------------+
                                |
                    if rounds_since_todo >= 3:  # 如果3轮没有更新todo
                      inject <reminder>       # 注入提醒

Key insight: "The agent can track its own progress -- and I can see it."  # 关键洞察：代理可以跟踪自己的进度 - 并且我可以看到它
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

SYSTEM = f"""You are a coding agent at {WORKDIR}.  # 系统提示：告诉AI是编码代理
Use the todo tool to plan multi-step tasks. Mark in_progress before starting, completed when done.  # 使用todo工具规划多步骤任务。开始前标记为in_progress，完成后标记为completed
Prefer tools over prose."""  # 优先使用工具而不是描述


# -- TodoManager: structured state the LLM writes to --  # 注释：TodoManager - LLM写入的结构化状态
class TodoManager:  # 定义TodoManager类
    def __init__(self):  # 初始化方法
        self.items = []  # 初始化待办事项列表为空

    def update(self, items: list) -> str:  # 更新待办事项的方法，接受列表参数，返回字符串
        if len(items) > 20:  # 如果待办事项超过20个
            raise ValueError("Max 20 todos allowed")  # 抛出异常：最多允许20个待办事项
        validated = []  # 初始化验证后的待办事项列表
        in_progress_count = 0  # 初始化进行中任务计数器
        for i, item in enumerate(items):  # 遍历待办事项列表
            text = str(item.get("text", "")).strip()  # 获取文本内容并去除首尾空白
            status = str(item.get("status", "pending")).lower()  # 获取状态，默认为pending
            item_id = str(item.get("id", str(i + 1)))  # 获取ID，默认为序号
            if not text:  # 如果没有文本内容
                raise ValueError(f"Item {item_id}: text required")  # 抛出异常：需要文本内容
            if status not in ("pending", "in_progress", "completed"):  # 如果状态不是有效状态
                raise ValueError(f"Item {item_id}: invalid status '{status}'")  # 抛出异常：无效状态
            if status == "in_progress":  # 如果状态是进行中
                in_progress_count += 1  # 增加进行中任务计数
            validated.append({"id": item_id, "text": text, "status": status})  # 将验证后的待办事项添加到列表
        if in_progress_count > 1:  # 如果有多个进行中任务
            raise ValueError("Only one task can be in_progress at a time")  # 抛出异常：同一时间只能有一个进行中任务
        self.items = validated  # 更新待办事项列表
        return self.render()  # 返回渲染后的待办事项列表

    def render(self) -> str:  # 渲染待办事项列表的方法
        if not self.items:  # 如果没有待办事项
            return "No todos."  # 返回无待办事项
        lines = []  # 初始化行列表
        for item in self.items:  # 遍历待办事项
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[item["status"]]  # 根据状态选择标记符号
            lines.append(f"{marker} #{item['id']}: {item['text']}")  # 添加格式化的待办事项行
        done = sum(1 for t in self.items if t["status"] == "completed")  # 计算已完成任务数量
        lines.append(f"\n({done}/{len(self.items)} completed)")  # 添加完成进度信息
        return "\n".join(lines)  # 返回合并后的字符串


TODO = TodoManager()  # 创建全局TodoManager实例


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
    "todo": lambda **kw: TODO.update(kw["items"]),  # todo更新处理器
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
    {"name": "todo", "description": "Update task list. Track progress on multi-step tasks.",  # todo工具定义
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object",
                                                                                            "properties": {"id": {
                                                                                                "type": "string"},
                                                                                                           "text": {
                                                                                                               "type": "string"},
                                                                                                           "status": {
                                                                                                               "type": "string",
                                                                                                               "enum": [
                                                                                                                   "pending",
                                                                                                                   "in_progress",
                                                                                                                   "completed"]}},
                                                                                            "required": ["id", "text",
                                                                                                         "status"]}}},
                      "required": ["items"]}},
]


# -- Agent loop with nag reminder injection --  # 注释：带有提醒注入的代理循环
def agent_loop(messages: list):  # 代理循环函数
    rounds_since_todo = 0  # 初始化自上次更新todo的轮数计数器
    while True:  # 无限循环
        # Nag reminder is injected below, alongside tool results  # 提醒将在下面与工具结果一起注入
        response = client.messages.create(  # 调用Anthropic API创建消息
            model=MODEL, system=SYSTEM, messages=messages,  # 指定模型、系统提示和消息历史
            tools=TOOLS, max_tokens=8000,  # 指定可用工具和最大token数
        )
        messages.append({"role": "assistant", "content": response.content})  # 追加助手的回复到消息历史
        if response.stop_reason != "tool_use":  # 如果模型没有调用工具
            return  # 结束循环
        results = []  # 初始化结果列表
        used_todo = False  # 初始化todo使用标志
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
                if block.name == "todo":  # 如果使用了todo工具
                    used_todo = True  # 设置todo使用标志为True
        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1  # 如果使用了todo则重置计数，否则增加计数
        if rounds_since_todo >= 3:  # 如果3轮没有更新todo
            results.append({"type": "text", "text": "<reminder>Update your todos.</reminder>"})  # 添加提醒消息
        messages.append({"role": "user", "content": results})  # 将结果作为用户消息追加到消息历史


if __name__ == "__main__":  # 如果是主程序入口
    history = []  # 初始化消息历史为空列表
    while True:  # 主交互循环
        try:
            query = input("\033[36ms03 >> \033[0m")  # 显示青色提示符并获取用户输入
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
