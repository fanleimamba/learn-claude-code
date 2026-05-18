#!/usr/bin/env python3  # shebang行，指定使用python3解释器执行此脚本
# Harness: team mailboxes -- multiple models, coordinated through files.  # 注释：团队邮箱 - 多个模型通过文件协调
"""
s09_agent_teams.py - Agent Teams  # 文档字符串：文件名和简要描述

Persistent named agents with file-based JSONL inboxes. Each teammate runs  # 持久化命名的代理，使用基于文件的JSONL收件箱。每个团队成员在自己的线程中运行代理循环
its own agent loop in a separate thread. Communication via append-only inboxes.  # 通过只追加的收件箱进行通信

    Subagent (s04):  spawn -> execute -> return summary -> destroyed  # 子代理（s04）：生成→执行→返回摘要→销毁
    Teammate (s09):  spawn -> work -> idle -> work -> ... -> shutdown  # 团队成员（s09）：生成→工作→空闲→工作→...→关闭

    .team/config.json                   .team/inbox/  # 团队配置文件和收件箱目录结构
    +----------------------------+      +------------------+
    | {"team_name": "default",   |      | alice.jsonl      |  # 团队成员alice的收件箱
    |  "members": [              |      | bob.jsonl        |  # 团队成员bob的收件箱
    |    {"name":"alice",        |      | lead.jsonl       |  # 团队领导lead的收件箱
    |     "role":"coder",        |      +------------------+  #
    |     "status":"idle"}       |  # 团队成员配置信息
    |  ]}                        |
    +----------------------------+      send_message("alice", "fix bug"):  # 发送消息给alice修复bug
                                        open("alice.jsonl", "a").write(msg)  # 向alice的收件箱追加消息

                                        read_inbox("alice"):  # 读取alice的收件箱
    spawn_teammate("alice","coder",...)   msgs = [json.loads(l) for l in ...]  # 解析JSONL消息
         |                                open("alice.jsonl", "w").close()  # 清空收件箱
         v                                return msgs  # drain  # 返回消息列表并清空
    Thread: alice             Thread: bob  # alice和bob的独立线程
    +------------------+      +------------------+
    | agent_loop       |      | agent_loop       |  # 各自的代理循环
    | status: working  |      | status: idle     |  # 工作状态和空闲状态
    | ... runs tools   |      | ... waits ...    |  # 执行工具和等待消息
    | status -> idle   |      |                  |  # 状态转换
    +------------------+      +------------------+

    5 message types (all declared, not all handled here):  # 5种消息类型（全部声明，但并非全部在此处理）：
    +-------------------------+-----------------------------------+  # 消息类型说明
    | message                 | Normal text message               |  # 普通文本消息
    | broadcast               | Sent to all teammates             |  # 广播给所有团队成员
    | shutdown_request        | Request graceful shutdown (s10)   |  # 优雅关闭请求（s10）
    | shutdown_response       | Approve/reject shutdown (s10)     |  # 关闭响应批准/拒绝（s10）
    | plan_approval_response  | Approve/reject plan (s10)         |  # 计划批准响应（s10）
    +-------------------------+-----------------------------------+

Key insight: "Teammates that can talk to each other."  # 关键洞察：可以相互交流的团队成员
"""

import json  # 导入JSON处理模块
import os  # 导入操作系统接口模块
import subprocess  # 导入子进程管理模块
import threading  # 导入线程管理模块
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
TEAM_DIR = WORKDIR / ".team"  # 设置团队目录路径
INBOX_DIR = TEAM_DIR / "inbox"  # 设置收件箱目录路径

SYSTEM = f"You are a team lead at {WORKDIR}. Spawn teammates and communicate via inboxes."  # 系统提示：告诉AI是团队领导，通过收件箱与团队成员沟通

VALID_MSG_TYPES = {  # 定义有效的消息类型集合
    "message",  # 普通消息
    "broadcast",  # 广播消息
    "shutdown_request",  # 关闭请求
    "shutdown_response",  # 关闭响应
    "plan_approval_response",  # 计划批准响应
}


# -- MessageBus: JSONL inbox per teammate --  # 注释：MessageBus：每个团队成员的JSONL收件箱
class MessageBus:  # 定义消息总线类
    def __init__(self, inbox_dir: Path):  # 初始化方法
        self.dir = inbox_dir  # 设置收件箱目录
        self.dir.mkdir(parents=True, exist_ok=True)  # 创建收件箱目录（如果不存在）

    def send(self, sender: str, to: str, content: str,  # 发送消息的方法
             msg_type: str = "message", extra: dict = None) -> str:
        if msg_type not in VALID_MSG_TYPES:  # 验证消息类型有效性
            return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"  # 返回错误信息
        msg = {  # 构建消息字典
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),  # 添加时间戳
        }
        if extra:  # 如果有额外信息
            msg.update(extra)  # 合并到消息中
        inbox_path = self.dir / f"{to}.jsonl"  # 构建收件箱文件路径
        with open(inbox_path, "a") as f:  # 以追加模式打开文件
            f.write(json.dumps(msg) + "\n")  # 写入JSON格式的消息
        return f"Sent {msg_type} to {to}"  # 返回发送成功信息

    def read_inbox(self, name: str) -> list:  # 读取收件箱的方法
        inbox_path = self.dir / f"{name}.jsonl"  # 构建收件箱文件路径
        if not inbox_path.exists():  # 如果文件不存在
            return []  # 返回空列表
        messages = []  # 初始化消息列表
        for line in inbox_path.read_text().strip().splitlines():  # 读取文件内容并按行分割
            if line:  # 如果行不为空
                messages.append(json.loads(line))  # 解析JSON并添加到消息列表
        inbox_path.write_text("")  # 清空文件内容
        return messages  # 返回消息列表

    def broadcast(self, sender: str, content: str, teammates: list) -> str:  # 广播消息的方法
        count = 0  # 初始化计数器
        for name in teammates:  # 遍历团队成员
            if name != sender:  # 如果不是发送者自己
                self.send(sender, name, content, "broadcast")  # 发送广播消息
                count += 1  # 增加计数器
        return f"Broadcast to {count} teammates"  # 返回广播成功信息


BUS = MessageBus(INBOX_DIR)  # 创建全局消息总线实例


# -- TeammateManager: persistent named agents with config.json --  # 注释：TeammateManager：带有config.json的持久化命名代理
class TeammateManager:  # 定义团队成员管理器类
    def __init__(self, team_dir: Path):  # 初始化方法
        self.dir = team_dir  # 设置团队目录
        self.dir.mkdir(exist_ok=True)  # 创建团队目录（如果不存在）
        self.config_path = self.dir / "config.json"  # 设置配置文件路径
        self.config = self._load_config()  # 加载配置
        self.threads = {}  # 初始化线程字典

    def _load_config(self) -> dict:  # 加载配置的方法
        if self.config_path.exists():  # 如果配置文件存在
            return json.loads(self.config_path.read_text())  # 读取并解析JSON配置
        return {"team_name": "default", "members": []}  # 返回默认配置

    def _save_config(self):  # 保存配置的方法
        self.config_path.write_text(json.dumps(self.config, indent=2))  # 将配置写入JSON文件

    def _find_member(self, name: str) -> dict:  # 查找成员的方法
        for m in self.config["members"]:  # 遍历团队成员
            if m["name"] == name:  # 如果找到匹配的成员
                return m  # 返回成员信息
        return None  # 返回None表示未找到

    def spawn(self, name: str, role: str, prompt: str) -> str:  # 生成团队成员的方法
        member = self._find_member(name)  # 查找成员
        if member:  # 如果成员已存在
            if member["status"] not in ("idle", "shutdown"):  # 如果成员状态不是空闲或关闭
                return f"Error: '{name}' is currently {member['status']}"  # 返回错误信息
            member["status"] = "working"  # 更新状态为工作中
            member["role"] = role  # 更新角色
        else:  # 如果成员不存在
            member = {"name": name, "role": role, "status": "working"}  # 创建新成员
            self.config["members"].append(member)  # 添加到团队成员列表
        self._save_config()  # 保存配置
        thread = threading.Thread(  # 创建新线程
            target=self._teammate_loop,  # 设置线程目标函数
            args=(name, role, prompt),  # 设置线程参数
            daemon=True,  # 设置为守护线程
        )
        self.threads[name] = thread  # 存储线程引用
        thread.start()  # 启动线程
        return f"Spawned '{name}' (role: {role})"  # 返回生成成功信息

    def _teammate_loop(self, name: str, role: str, prompt: str):  # 团队成员循环方法
        sys_prompt = (  # 构建系统提示
            f"You are '{name}', role: {role}, at {WORKDIR}. "
            f"Use send_message to communicate. Complete your task."
        )
        messages = [{"role": "user", "content": prompt}]  # 初始化消息列表
        tools = self._teammate_tools()  # 获取团队成员可用工具
        for _ in range(50):  # 最多50轮对话
            inbox = BUS.read_inbox(name)  # 读取收件箱
            for msg in inbox:  # 遍历收件箱消息
                messages.append({"role": "user", "content": json.dumps(msg)})  # 将消息添加到对话历史
            try:
                response = client.messages.create(  # 调用LLM创建消息
                    model=MODEL,
                    system=sys_prompt,
                    messages=messages,
                    tools=tools,
                    max_tokens=8000,
                )
            except Exception:  # 捕获异常
                break  # 退出循环
            messages.append({"role": "assistant", "content": response.content})  # 追加助手的回复
            if response.stop_reason != "tool_use":  # 如果模型没有调用工具
                break  # 退出循环
            results = []  # 初始化结果列表
            for block in response.content:  # 遍历响应内容块
                if block.type == "tool_use":  # 如果是工具使用块
                    output = self._exec(name, block.name, block.input)  # 执行工具
                    print(f"  [{name}] {block.name}: {str(output)[:120]}")  # 打印执行信息
                    results.append({  # 将工具结果添加到结果列表
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(output),
                    })
            messages.append({"role": "user", "content": results})  # 将结果作为用户消息追加
        member = self._find_member(name)  # 查找成员
        if member and member["status"] != "shutdown":  # 如果成员存在且状态不是关闭
            member["status"] = "idle"  # 更新状态为空闲
            self._save_config()  # 保存配置

    def _exec(self, sender: str, tool_name: str, args: dict) -> str:  # 执行工具的方法
        # these base tools are unchanged from s02  # 这些基础工具与s02相同
        if tool_name == "bash":  # 如果是bash工具
            return _run_bash(args["command"])  # 运行bash命令
        if tool_name == "read_file":  # 如果是读取文件工具
            return _run_read(args["path"])  # 读取文件
        if tool_name == "write_file":  # 如果是写入文件工具
            return _run_write(args["path"], args["content"])  # 写入文件
        if tool_name == "edit_file":  # 如果是编辑文件工具
            return _run_edit(args["path"], args["old_text"], args["new_text"])  # 编辑文件
        if tool_name == "send_message":  # 如果是发送消息工具
            return BUS.send(sender, args["to"], args["content"], args.get("msg_type", "message"))  # 发送消息
        if tool_name == "read_inbox":  # 如果是读取收件箱工具
            return json.dumps(BUS.read_inbox(sender), indent=2)  # 读取并返回收件箱内容
        return f"Unknown tool: {tool_name}"  # 返回未知工具错误

    def _teammate_tools(self) -> list:  # 获取团队成员工具的方法
        # these base tools are unchanged from s02  # 这些基础工具与s02相同
        return [  # 返回团队成员可用工具列表
            {"name": "bash", "description": "Run a shell command.",  # bash工具定义
             "input_schema": {"type": "object", "properties": {"command": {"type": "string"}},
                              "required": ["command"]}},
            {"name": "read_file", "description": "Read file contents.",  # 读取文件工具定义
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            {"name": "write_file", "description": "Write content to file.",  # 写入文件工具定义
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                              "required": ["path", "content"]}},
            {"name": "edit_file", "description": "Replace exact text in file.",  # 编辑文件工具定义
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"}, "old_text": {"type": "string"},
                                             "new_text": {"type": "string"}},
                              "required": ["path", "old_text", "new_text"]}},
            {"name": "send_message", "description": "Send message to a teammate.",  # 发送消息工具定义
             "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"},
                                                               "msg_type": {"type": "string",
                                                                            "enum": list(VALID_MSG_TYPES)}},
                              "required": ["to", "content"]}},
            {"name": "read_inbox", "description": "Read and drain your inbox.",  # 读取收件箱工具定义
             "input_schema": {"type": "object", "properties": {}}},
        ]

    def list_all(self) -> str:  # 列出所有团队成员的方法
        if not self.config["members"]:  # 如果没有团队成员
            return "No teammates."  # 返回无团队成员
        lines = [f"Team: {self.config['team_name']}"]  # 初始化输出行列表
        for m in self.config["members"]:  # 遍历团队成员
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")  # 格式化成员信息
        return "\n".join(lines)  # 返回合并后的字符串

    def member_names(self) -> list:  # 获取成员名称列表的方法
        return [m["name"] for m in self.config["members"]]  # 返回所有成员名称


TEAM = TeammateManager(TEAM_DIR)  # 创建全局团队成员管理器实例


# -- Base tool implementations (these base tools are unchanged from s02) --  # 注释：基础工具实现（这些基础工具与s02相同）
def _safe_path(p: str) -> Path:  # 安全路径函数
    path = (WORKDIR / p).resolve()  # 解析相对路径
    if not path.is_relative_to(WORKDIR):  # 如果路径不在工作目录内
        raise ValueError(f"Path escapes workspace: {p}")  # 抛出异常
    return path  # 返回安全路径


def _run_bash(command: str) -> str:  # 运行bash命令的函数
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot"]  # 定义危险命令列表
    if any(d in command for d in dangerous):  # 如果命令包含危险指令
        return "Error: Dangerous command blocked"  # 返回错误：危险命令被阻止
    try:
        r = subprocess.run(  # 运行子进程
            command, shell=True, cwd=WORKDIR,  # 使用shell执行，设置工作目录
            capture_output=True, text=True, timeout=120,  # 捕获输出，文本模式，120秒超时
        )
        out = (r.stdout + r.stderr).strip()  # 合并标准输出和错误输出并去除首尾空白
        return out[:50000] if out else "(no output)"  # 返回前50000字符，若无输出则返回"(no output)"
    except subprocess.TimeoutExpired:  # 捕获超时异常
        return "Error: Timeout (120s)"  # 返回超时错误


def _run_read(path: str, limit: int = None) -> str:  # 读取文件内容的函数
    try:
        lines = _safe_path(path).read_text().splitlines()  # 读取文件内容并分割成行
        if limit and limit < len(lines):  # 如果设置了限制且行数超过限制
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]  # 截取前limit行并添加提示
        return "\n".join(lines)[:50000]  # 返回合并后的字符串，最多50000字符
    except Exception as e:  # 捕获异常
        return f"Error: {e}"  # 返回错误信息


def _run_write(path: str, content: str) -> str:  # 写入文件内容的函数
    try:
        fp = _safe_path(path)  # 获取安全路径
        fp.parent.mkdir(parents=True, exist_ok=True)  # 创建父目录
        fp.write_text(content)  # 写入内容
        return f"Wrote {len(content)} bytes"  # 返回写入字节数
    except Exception as e:  # 捕获异常
        return f"Error: {e}"  # 返回错误信息


def _run_edit(path: str, old_text: str, new_text: str) -> str:  # 编辑文件内容的函数
    try:
        fp = _safe_path(path)  # 获取安全路径
        c = fp.read_text()  # 读取文件内容
        if old_text not in c:  # 如果要替换的文本不存在
            return f"Error: Text not found in {path}"  # 返回错误：文本未找到
        fp.write_text(c.replace(old_text, new_text, 1))  # 替换文本并写入
        return f"Edited {path}"  # 返回编辑成功信息
    except Exception as e:  # 捕获异常
        return f"Error: {e}"  # 返回错误信息


# -- Lead tool dispatch (9 tools) --  # 注释：领导工具分发（9个工具）
TOOL_HANDLERS = {  # 工具处理器映射
    "bash": lambda **kw: _run_bash(kw["command"]),  # bash命令处理器
    "read_file": lambda **kw: _run_read(kw["path"], kw.get("limit")),  # 读取文件处理器
    "write_file": lambda **kw: _run_write(kw["path"], kw["content"]),  # 写入文件处理器
    "edit_file": lambda **kw: _run_edit(kw["path"], kw["old_text"], kw["new_text"]),  # 编辑文件处理器
    "spawn_teammate": lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),  # 生成团队成员处理器
    "list_teammates": lambda **kw: TEAM.list_all(),  # 列出团队成员处理器
    "send_message": lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),  # 发送消息处理器
    "read_inbox": lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),  # 读取收件箱处理器
    "broadcast": lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),  # 广播消息处理器
}

# these base tools are unchanged from s02  # 这些基础工具与s02相同
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
    {"name": "spawn_teammate", "description": "Spawn a persistent teammate that runs in its own thread.",  # 生成团队成员工具定义
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"},
                                                       "prompt": {"type": "string"}},
                      "required": ["name", "role", "prompt"]}},
    {"name": "list_teammates", "description": "List all teammates with name, role, status.",  # 列出团队成员工具定义
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "send_message", "description": "Send a message to a teammate's inbox.",  # 发送消息工具定义
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"},
                                                       "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}},
                      "required": ["to", "content"]}},
    {"name": "read_inbox", "description": "Read and drain the lead's inbox.",  # 读取收件箱工具定义
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "broadcast", "description": "Send a message to all teammates.",  # 广播消息工具定义
     "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
]


def agent_loop(messages: list):  # 代理循环函数
    inbox = BUS.read_inbox("lead")  # 读取领导的收件箱
    if inbox:  # 如果有消息
        messages.append({  # 将收件箱消息添加到对话历史
            "role": "user",
            "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>",
        })
    response = client.messages.create(  # 调用LLM创建消息
        model=MODEL,
        system=SYSTEM,
        messages=messages,
        tools=TOOLS,
        max_tokens=8000,
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
            results.append({  # 将工具结果添加到结果列表
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(output),
            })
    messages.append({"role": "user", "content": results})  # 将结果作为用户消息追加到消息历史


if __name__ == "__main__":  # 如果是主程序入口
    history = []  # 初始化消息历史为空列表
    while True:  # 主交互循环
        try:
            query = input("\033[36ms09 >> \033[0m")  # 显示青色提示符并获取用户输入
        except (EOFError, KeyboardInterrupt):  # 捕获文件结束或键盘中断异常
            break  # 退出循环
        if query.strip().lower() in ("q", "exit", ""):  # 如果输入为空或"q"/"exit"
            break  # 退出循环
        if query.strip() == "/team":  # 如果是/team命令
            print(TEAM.list_all())  # 打印团队成员列表
            continue  # 继续下一次循环
        if query.strip() == "/inbox":  # 如果是/inbox命令
            print(json.dumps(BUS.read_inbox("lead"), indent=2))  # 打印领导的收件箱内容
            continue  # 继续下一次循环
        history.append({"role": "user", "content": query})  # 将用户查询添加到消息历史
        agent_loop(history)  # 调用代理循环处理查询
        response_content = history[-1]["content"]  # 获取最后一条消息内容
        if isinstance(response_content, list):  # 如果内容是一个列表
            for block in response_content:  # 遍历内容块
                if hasattr(block, "text"):  # 如果块有text属性
                    print(block.text)  # 打印文本内容
        print()  # 打印空行分隔
