#!/usr/bin/env python3  # Python解释器路径声明，确保脚本使用Python3执行
# Harness: autonomy -- models that find work without being told.  # 自治框架：无需指令即可寻找工作的模型
"""
s11_autonomous_agents.py - Autonomous Agents  # 自主代理模块

Idle cycle with task board polling, auto-claiming unclaimed tasks, and  # 空闲循环配合任务板轮询，自动认领未认领任务
identity re-injection after context compression. Builds on s10's protocols.  # 上下文压缩后重新注入身份信息。基于s10的协议构建

    Teammate lifecycle:  # 团队成员生命周期：
    +-------+  # +-------+
    | spawn |  # | 创建  |
    +---+---+  # +---+---+
        |  #     |
        v  #     v
    +-------+  tool_use    +-------+  # +-------+  工具使用    +-------+
    | WORK  | <----------- |  LLM  |  # | 工作  | <----------- |  LLM  |
    +---+---+              +-------+  # +---+---+              +-------+
        |  #     |
        | stop_reason != tool_use  # 停止原因不是工具使用
        v  #     v
    +--------+  # +--------+
    | IDLE   | poll every 5s for up to 60s  # | 空闲   | 每5秒轮询一次，最多60秒
    +---+----+  # +---+----+
        |  #     |
        +---> check inbox -> message? -> resume WORK  # +---> 检查收件箱 -> 有消息？ -> 恢复工作
        |  #     |
        +---> scan .tasks/ -> unclaimed? -> claim -> resume WORK  # +---> 扫描.tasks/ -> 有未认领？ -> 认领 -> 恢复工作
        |  #     |
        +---> timeout (60s) -> shutdown  # +---> 超时（60秒） -> 关闭

    Identity re-injection after compression:  # 压缩后重新注入身份：
    messages = [identity_block, ...remaining...]  # 消息 = [身份块, ...剩余...]
    "You are 'coder', role: backend, team: my-team"  # "你是'coder'，角色：后端，团队：my-team"

Key insight: "The agent finds work itself."  # 关键洞察："代理自己找到工作"
"""  # 文档字符串结束

import json  # 导入JSON处理模块
import os  # 导入操作系统接口模块
import subprocess  # 导入子进程管理模块
import threading  # 导入线程管理模块
import time  # 导入时间处理模块
import uuid  # 导入UUID生成模块
from pathlib import Path  # 从pathlib导入Path类，用于路径操作

from anthropic import Anthropic  # 从anthropic导入Anthropic类，用于AI客户端
from dotenv import load_dotenv  # 从dotenv导入load_dotenv，用于加载环境变量

load_dotenv(override=True)  # 加载环境变量，覆盖现有的环境变量
if os.getenv("ANTHROPIC_BASE_URL"):  # 如果设置了ANTHROPIC_BASE_URL环境变量
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)  # 移除ANTHROPIC_AUTH_TOKEN环境变量

WORKDIR = Path.cwd()  # 设置工作目录为当前工作目录
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # 创建Anthropic客户端实例，使用环境变量中的基础URL
MODEL = os.environ["MODEL_ID"]  # 从环境变量获取模型ID
TEAM_DIR = WORKDIR / ".team"  # 团队目录路径设置为工作目录下的.team文件夹
INBOX_DIR = TEAM_DIR / "inbox"  # 收件箱目录路径设置为团队目录下的inbox文件夹
TASKS_DIR = WORKDIR / ".tasks"  # 任务目录路径设置为工作目录下的.tasks文件夹

POLL_INTERVAL = 5  # 轮询间隔设置为5秒
IDLE_TIMEOUT = 60  # 空闲超时设置为60秒

SYSTEM = f"You are a team lead at {WORKDIR}. Teammates are autonomous -- they find work themselves."  # 系统提示语，定义团队领导的角色和职责

VALID_MSG_TYPES = {  # 定义有效的消息类型集合
    "message",  # 普通消息
    "broadcast",  # 广播消息
    "shutdown_request",  # 关闭请求
    "shutdown_response",  # 关闭响应
    "plan_approval_response",  # 计划批准响应
}

# -- Request trackers --  # -- 请求跟踪器 --
shutdown_requests = {}  # 关闭请求跟踪字典，存储request_id到请求详情的映射
plan_requests = {}  # 计划请求跟踪字典，存储request_id到计划详情的映射
_tracker_lock = threading.Lock()  # 创建线程锁，用于保护请求跟踪器的线程安全访问
_claim_lock = threading.Lock()  # 创建线程锁，用于保护任务认领的线程安全访问


# -- MessageBus: JSONL inbox per teammate --  # -- 消息总线：每个团队成员的JSONL收件箱 --
class MessageBus:  # 定义消息总线类
    def __init__(self, inbox_dir: Path):  # 初始化方法，接收收件箱目录路径
        self.dir = inbox_dir  # 存储收件箱目录路径
        self.dir.mkdir(parents=True, exist_ok=True)  # 创建收件箱目录（如果不存在）

    def send(self, sender: str, to: str, content: str,  # 发送消息方法
             msg_type: str = "message", extra: dict = None) -> str:  # 参数：发送者、接收者、内容、消息类型、额外数据
        if msg_type not in VALID_MSG_TYPES:  # 如果消息类型不在有效类型集合中
            return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"  # 返回错误信息
        msg = {  # 构建消息字典
            "type": msg_type,  # 消息类型
            "from": sender,  # 发送者
            "content": content,  # 消息内容
            "timestamp": time.time(),  # 时间戳
        }
        if extra:  # 如果有额外数据
            msg.update(extra)  # 将额外数据合并到消息中
        inbox_path = self.dir / f"{to}.jsonl"  # 构建收件箱文件路径
        with open(inbox_path, "a") as f:  # 以追加模式打开收件箱文件
            f.write(json.dumps(msg) + "\n")  # 将消息序列化为JSON并写入文件，添加换行符
        return f"Sent {msg_type} to {to}"  # 返回发送成功信息

    def read_inbox(self, name: str) -> list:  # 读取收件箱方法
        inbox_path = self.dir / f"{name}.jsonl"  # 构建收件箱文件路径
        if not inbox_path.exists():  # 如果收件箱文件不存在
            return []  # 返回空列表
        messages = []  # 初始化消息列表
        for line in inbox_path.read_text().strip().splitlines():  # 读取文件内容并按行分割
            if line:  # 如果行不为空
                messages.append(json.loads(line))  # 解析JSON行并添加到消息列表
        inbox_path.write_text("")  # 清空收件箱文件内容
        return messages  # 返回消息列表

    def broadcast(self, sender: str, content: str, teammates: list) -> str:  # 广播消息方法
        count = 0  # 初始化计数器
        for name in teammates:  # 遍历团队成员列表
            if name != sender:  # 如果不是发送者本人
                self.send(sender, name, content, "broadcast")  # 发送广播消息
                count += 1  # 增加计数
        return f"Broadcast to {count} teammates"  # 返回广播结果


BUS = MessageBus(INBOX_DIR)  # 创建全局消息总线实例


# -- Task board scanning --  # -- 任务板扫描 --
def scan_unclaimed_tasks() -> list:  # 扫描未认领任务函数
    TASKS_DIR.mkdir(exist_ok=True)  # 创建任务目录（如果不存在）
    unclaimed = []  # 初始化未认领任务列表
    for f in sorted(TASKS_DIR.glob("task_*.json")):  # 遍历所有任务文件
        task = json.loads(f.read_text())  # 读取并解析任务文件
        if (task.get("status") == "pending"  # 如果任务状态为待处理
                and not task.get("owner")  # 且没有所有者
                and not task.get("blockedBy")):  # 且没有被其他任务阻塞
            unclaimed.append(task)  # 添加到未认领任务列表
    return unclaimed  # 返回未认领任务列表


def claim_task(task_id: int, owner: str) -> str:  # 认领任务函数
    with _claim_lock:  # 获取线程锁
        path = TASKS_DIR / f"task_{task_id}.json"  # 构建任务文件路径
        if not path.exists():  # 如果任务文件不存在
            return f"Error: Task {task_id} not found"  # 返回错误信息
        task = json.loads(path.read_text())  # 读取并解析任务文件
        if task.get("owner"):  # 如果任务已有所有者
            existing_owner = task.get("owner") or "someone else"  # 获取现有所有者
            return f"Error: Task {task_id} has already been claimed by {existing_owner}"  # 返回错误信息
        if task.get("status") != "pending":  # 如果任务状态不是待处理
            status = task.get("status")  # 获取任务状态
            return f"Error: Task {task_id} cannot be claimed because its status is '{status}'"  # 返回错误信息
        if task.get("blockedBy"):  # 如果任务被其他任务阻塞
            return f"Error: Task {task_id} is blocked by other task(s) and cannot be claimed yet"  # 返回错误信息
        task["owner"] = owner  # 设置任务所有者
        task["status"] = "in_progress"  # 设置任务状态为进行中
        path.write_text(json.dumps(task, indent=2))  # 将更新后的任务写入文件
    return f"Claimed task #{task_id} for {owner}"  # 返回认领成功信息


# -- Identity re-injection after compression --  # -- 压缩后重新注入身份 --
def make_identity_block(name: str, role: str, team_name: str) -> dict:  # 创建身份块函数
    return {  # 返回身份块字典
        "role": "user",  # 角色为用户
        "content": f"<identity>You are '{name}', role: {role}, team: {team_name}. Continue your work.</identity>",
        # 身份内容
    }


# -- Autonomous TeammateManager --  # -- 自主团队成员管理器 --
class TeammateManager:  # 定义自主团队成员管理器类
    def __init__(self, team_dir: Path):  # 初始化方法，接收团队目录路径
        self.dir = team_dir  # 存储团队目录路径
        self.dir.mkdir(exist_ok=True)  # 创建团队目录（如果不存在）
        self.config_path = self.dir / "config.json"  # 配置文件路径
        self.config = self._load_config()  # 加载配置
        self.threads = {}  # 初始化线程字典，存储团队成员线程

    def _load_config(self) -> dict:  # 加载配置方法
        if self.config_path.exists():  # 如果配置文件存在
            return json.loads(self.config_path.read_text())  # 读取并解析JSON配置文件
        return {"team_name": "default", "members": []}  # 返回默认配置

    def _save_config(self):  # 保存配置方法
        self.config_path.write_text(json.dumps(self.config, indent=2))  # 将配置序列化为JSON并写入文件

    def _find_member(self, name: str) -> dict:  # 查找成员方法
        for m in self.config["members"]:  # 遍历成员列表
            if m["name"] == name:  # 如果找到匹配的成员名
                return m  # 返回成员信息
        return None  # 未找到返回None

    def _set_status(self, name: str, status: str):  # 设置成员状态方法
        member = self._find_member(name)  # 查找成员
        if member:  # 如果找到成员
            member["status"] = status  # 设置成员状态
            self._save_config()  # 保存配置

    def spawn(self, name: str, role: str, prompt: str) -> str:  # 创建成员方法
        member = self._find_member(name)  # 查找成员
        if member:  # 如果成员已存在
            if member["status"] not in ("idle", "shutdown"):  # 如果成员状态不是空闲或关闭
                return f"Error: '{name}' is currently {member['status']}"  # 返回错误信息
            member["status"] = "working"  # 设置成员状态为工作中
            member["role"] = role  # 更新成员角色
        else:  # 如果成员不存在
            member = {"name": name, "role": role, "status": "working"}  # 创建新成员
            self.config["members"].append(member)  # 添加到成员列表
        self._save_config()  # 保存配置
        thread = threading.Thread(  # 创建新线程
            target=self._loop,  # 线程目标函数
            args=(name, role, prompt),  # 线程参数
            daemon=True,  # 设置为守护线程
        )
        self.threads[name] = thread  # 存储线程引用
        thread.start()  # 启动线程
        return f"Spawned '{name}' (role: {role})"  # 返回创建成功信息

    def _loop(self, name: str, role: str, prompt: str):  # 主循环方法
        team_name = self.config["team_name"]  # 获取团队名称
        sys_prompt = (  # 构建系统提示
            f"You are '{name}', role: {role}, team: {team_name}, at {WORKDIR}. "  # 角色定义
            f"Use idle tool when you have no more work. You will auto-claim new tasks."  # 无工作时使用空闲工具，会自动认领新任务
        )
        messages = [{"role": "user", "content": prompt}]  # 初始化消息列表
        tools = self._teammate_tools()  # 获取可用工具列表

        while True:  # 无限循环
            # -- WORK PHASE: standard agent loop --  # -- 工作阶段：标准代理循环 --
            for _ in range(50):  # 最多循环50次
                inbox = BUS.read_inbox(name)  # 读取收件箱
                for msg in inbox:  # 处理收件箱中的每条消息
                    if msg.get("type") == "shutdown_request":  # 如果是关闭请求
                        self._set_status(name, "shutdown")  # 设置成员状态为关闭
                        return  # 退出循环
                    messages.append({"role": "user", "content": json.dumps(msg)})  # 将消息添加到对话历史中
                try:  # 尝试调用AI模型
                    response = client.messages.create(  # 创建消息请求
                        model=MODEL,  # 使用配置的模型
                        system=sys_prompt,  # 系统提示
                        messages=messages,  # 对话历史
                        tools=tools,  # 可用工具
                        max_tokens=8000,  # 最大token数
                    )
                except Exception:  # 如果发生异常
                    self._set_status(name, "idle")  # 设置成员状态为空闲
                    return  # 退出循环
                messages.append({"role": "assistant", "content": response.content})  # 将AI响应添加到对话历史
                if response.stop_reason != "tool_use":  # 如果停止原因不是工具使用
                    break  # 退出循环
                results = []  # 初始化结果列表
                idle_requested = False  # 空闲请求标志
                for block in response.content:  # 遍历响应内容块
                    if block.type == "tool_use":  # 如果是工具使用块
                        if block.name == "idle":  # 如果是空闲工具
                            idle_requested = True  # 设置空闲请求标志
                            output = "Entering idle phase. Will poll for new tasks."  # 空闲阶段输出
                        else:  # 如果是其他工具
                            output = self._exec(name, block.name, block.input)  # 执行工具
                        print(f"  [{name}] {block.name}: {str(output)[:120]}")  # 打印执行结果（截断）
                        results.append({  # 构建工具结果
                            "type": "tool_result",  # 结果类型
                            "tool_use_id": block.id,  # 工具使用ID
                            "content": str(output),  # 结果内容
                        })
                messages.append({"role": "user", "content": results})  # 将工具结果添加到对话历史
                if idle_requested:  # 如果请求了空闲
                    break  # 退出循环

            # -- IDLE PHASE: poll for inbox messages and unclaimed tasks --  # -- 空闲阶段：轮询收件箱消息和未认领任务 --
            self._set_status(name, "idle")  # 设置成员状态为空闲
            resume = False  # 恢复标志
            polls = IDLE_TIMEOUT // max(POLL_INTERVAL, 1)  # 计算轮询次数
            for _ in range(polls):  # 轮询指定次数
                time.sleep(POLL_INTERVAL)  # 睡眠轮询间隔时间
                inbox = BUS.read_inbox(name)  # 读取收件箱
                if inbox:  # 如果收件箱不为空
                    for msg in inbox:  # 处理收件箱中的每条消息
                        if msg.get("type") == "shutdown_request":  # 如果是关闭请求
                            self._set_status(name, "shutdown")  # 设置成员状态为关闭
                            return  # 退出循环
                        messages.append({"role": "user", "content": json.dumps(msg)})  # 将消息添加到对话历史中
                    resume = True  # 设置恢复标志
                    break  # 退出轮询循环
                unclaimed = scan_unclaimed_tasks()  # 扫描未认领任务
                if unclaimed:  # 如果有未认领任务
                    task = unclaimed[0]  # 获取第一个未认领任务
                    result = claim_task(task["id"], name)  # 尝试认领任务
                    if result.startswith("Error:"):  # 如果认领失败
                        continue  # 继续轮询
                    task_prompt = (  # 构建任务提示
                        f"<auto-claimed>Task #{task['id']}: {task['subject']}\n"  # 自动认领的任务信息
                        f"{task.get('description', '')}</auto-claimed>"  # 任务描述
                    )
                    if len(messages) <= 3:  # 如果消息数量较少
                        messages.insert(0, make_identity_block(name, role, team_name))  # 在开头插入身份块
                        messages.insert(1, {"role": "assistant", "content": f"I am {name}. Continuing."})  # 插入继续对话
                    messages.append({"role": "user", "content": task_prompt})  # 添加任务提示到对话历史
                    messages.append(
                        {"role": "assistant", "content": f"Claimed task #{task['id']}. Working on it."})  # 添加认领确认
                    resume = True  # 设置恢复标志
                    break  # 退出轮询循环

            if not resume:  # 如果没有恢复
                self._set_status(name, "shutdown")  # 设置成员状态为关闭
                return  # 退出循环
            self._set_status(name, "working")  # 设置成员状态为工作中

    def _exec(self, sender: str, tool_name: str, args: dict) -> str:  # 执行工具方法
        # these base tools are unchanged from s02  # 这些基础工具与s02版本相同
        if tool_name == "bash":  # 如果是bash命令
            return _run_bash(args["command"])  # 执行bash命令
        if tool_name == "read_file":  # 如果是读取文件
            return _run_read(args["path"])  # 读取文件内容
        if tool_name == "write_file":  # 如果是写入文件
            return _run_write(args["path"], args["content"])  # 写入文件内容
        if tool_name == "edit_file":  # 如果是编辑文件
            return _run_edit(args["path"], args["old_text"], args["new_text"])  # 编辑文件内容
        if tool_name == "send_message":  # 如果是发送消息
            return BUS.send(sender, args["to"], args["content"], args.get("msg_type", "message"))  # 发送消息
        if tool_name == "read_inbox":  # 如果是读取收件箱
            return json.dumps(BUS.read_inbox(sender), indent=2)  # 读取并格式化收件箱内容
        if tool_name == "shutdown_response":  # 如果是关闭响应
            req_id = args["request_id"]  # 获取请求ID
            with _tracker_lock:  # 获取线程锁
                if req_id in shutdown_requests:  # 如果请求ID存在于关闭请求中
                    shutdown_requests[req_id]["status"] = "approved" if args["approve"] else "rejected"  # 更新状态
            BUS.send(  # 发送响应消息
                sender, "lead", args.get("reason", ""),  # 发送给领导者，包含原因
                "shutdown_response", {"request_id": req_id, "approve": args["approve"]},  # 消息类型和额外数据
            )
            return f"Shutdown {'approved' if args['approve'] else 'rejected'}"  # 返回关闭结果
        if tool_name == "plan_approval":  # 如果是计划批准
            plan_text = args.get("plan", "")  # 获取计划文本
            req_id = str(uuid.uuid4())[:8]  # 生成请求ID
            with _tracker_lock:  # 获取线程锁
                plan_requests[req_id] = {"from": sender, "plan": plan_text, "status": "pending"}  # 添加计划请求
            BUS.send(  # 发送计划消息
                sender, "lead", plan_text, "plan_approval_response",  # 发送给领导者
                {"request_id": req_id, "plan": plan_text},  # 包含请求ID和计划文本
            )
            return f"Plan submitted (request_id={req_id}). Waiting for approval."  # 返回提交结果
        if tool_name == "claim_task":  # 如果是认领任务
            return claim_task(args["task_id"], sender)  # 认领任务
        return f"Unknown tool: {tool_name}"  # 返回未知工具错误

    def _teammate_tools(self) -> list:  # 获取团队成员工具列表方法
        # these base tools are unchanged from s02  # 这些基础工具与s02版本相同
        return [  # 返回工具列表
            {"name": "bash", "description": "Run a shell command.",  # bash命令工具
             "input_schema": {"type": "object", "properties": {"command": {"type": "string"}},
                              "required": ["command"]}},  # 输入模式
            {"name": "read_file", "description": "Read file contents.",  # 读取文件工具
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            # 输入模式
            {"name": "write_file", "description": "Write content to file.",  # 写入文件工具
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                              "required": ["path", "content"]}},  # 输入模式
            {"name": "edit_file", "description": "Replace exact text in file.",  # 编辑文件工具
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"}, "old_text": {"type": "string"},
                                             "new_text": {"type": "string"}},
                              "required": ["path", "old_text", "new_text"]}},  # 输入模式
            {"name": "send_message", "description": "Send message to a teammate.",  # 发送消息工具
             "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"},
                                                               "msg_type": {"type": "string",
                                                                            "enum": list(VALID_MSG_TYPES)}},
                              "required": ["to", "content"]}},  # 输入模式
            {"name": "read_inbox", "description": "Read and drain your inbox.",  # 读取收件箱工具
             "input_schema": {"type": "object", "properties": {}}},  # 输入模式
            {"name": "shutdown_response", "description": "Respond to a shutdown request.",  # 关闭响应工具
             "input_schema": {"type": "object",
                              "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"},
                                             "reason": {"type": "string"}}, "required": ["request_id", "approve"]}},
            # 输入模式
            {"name": "plan_approval", "description": "Submit a plan for lead approval.",  # 计划批准工具
             "input_schema": {"type": "object", "properties": {"plan": {"type": "string"}}, "required": ["plan"]}},
            # 输入模式
            {"name": "idle", "description": "Signal that you have no more work. Enters idle polling phase.",  # 空闲工具
             "input_schema": {"type": "object", "properties": {}}},  # 输入模式
            {"name": "claim_task", "description": "Claim a task from the task board by ID.",  # 认领任务工具
             "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}},
                              "required": ["task_id"]}},  # 输入模式
        ]

    def list_all(self) -> str:  # 列出所有成员方法
        if not self.config["members"]:  # 如果没有成员
            return "No teammates."  # 返回无团队成员信息
        lines = [f"Team: {self.config['team_name']}"]  # 构建团队名称行
        for m in self.config["members"]:  # 遍历成员列表
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")  # 添加成员信息行
        return "\n".join(lines)  # 连接所有行并返回

    def member_names(self) -> list:  # 获取成员名称列表方法
        return [m["name"] for m in self.config["members"]]  # 返回成员名称列表


TEAM = TeammateManager(TEAM_DIR)  # 创建全局团队成员管理器实例


# -- Base tool implementations (these base tools are unchanged from s02) --  # -- 基础工具实现（这些基础工具与s02版本相同） --
def _safe_path(p: str) -> Path:  # 安全路径处理方法
    path = (WORKDIR / p).resolve()  # 解析相对路径为绝对路径
    if not path.is_relative_to(WORKDIR):  # 如果路径不在工作目录内
        raise ValueError(f"Path escapes workspace: {p}")  # 抛出路径越界错误
    return path  # 返回安全路径


def _run_bash(command: str) -> str:  # 执行bash命令方法
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot"]  # 定义危险命令列表
    if any(d in command for d in dangerous):  # 如果命令包含危险指令
        return "Error: Dangerous command blocked"  # 返回危险命令被阻止错误
    try:  # 尝试执行命令
        r = subprocess.run(  # 运行子进程
            command, shell=True, cwd=WORKDIR,  # 使用shell执行，设置工作目录
            capture_output=True, text=True, timeout=120,  # 捕获输出，文本模式，超时120秒
        )
        out = (r.stdout + r.stderr).strip()  # 合并标准输出和错误输出并去除空白
        return out[:50000] if out else "(no output)"  # 返回输出（截断）或无输出提示
    except subprocess.TimeoutExpired:  # 如果超时
        return "Error: Timeout (120s)"  # 返回超时错误


def _run_read(path: str, limit: int = None) -> str:  # 读取文件方法
    try:  # 尝试读取文件
        lines = _safe_path(path).read_text().splitlines()  # 安全读取文件并按行分割
        if limit and limit < len(lines):  # 如果设置了限制且行数超过限制
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]  # 截取前limit行并添加提示
        return "\n".join(lines)[:50000]  # 连接行并返回（截断）
    except Exception as e:  # 如果发生异常
        return f"Error: {e}"  # 返回错误信息


def _run_write(path: str, content: str) -> str:  # 写入文件方法
    try:  # 尝试写入文件
        fp = _safe_path(path)  # 获取安全路径
        fp.parent.mkdir(parents=True, exist_ok=True)  # 创建父目录（如果不存在）
        fp.write_text(content)  # 写入内容
        return f"Wrote {len(content)} bytes"  # 返回写入字节数
    except Exception as e:  # 如果发生异常
        return f"Error: {e}"  # 返回错误信息


def _run_edit(path: str, old_text: str, new_text: str) -> str:  # 编辑文件方法
    try:  # 尝试编辑文件
        fp = _safe_path(path)  # 获取安全路径
        c = fp.read_text()  # 读取文件内容
        if old_text not in c:  # 如果要替换的文本不存在
            return f"Error: Text not found in {path}"  # 返回未找到文本错误
        fp.write_text(c.replace(old_text, new_text, 1))  # 替换文本并写入
        return f"Edited {path}"  # 返回编辑成功信息
    except Exception as e:  # 如果发生异常
        return f"Error: {e}"  # 返回错误信息


# -- Lead-specific protocol handlers --  # -- 领导者特定协议处理器 --
def handle_shutdown_request(teammate: str) -> str:  # 处理关闭请求方法
    req_id = str(uuid.uuid4())[:8]  # 生成8位请求ID
    with _tracker_lock:  # 获取线程锁
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}  # 添加关闭请求到跟踪器
    BUS.send(  # 发送关闭请求消息
        "lead", teammate, "Please shut down gracefully.",  # 发送给指定团队成员
        "shutdown_request", {"request_id": req_id},  # 消息类型和请求ID
    )
    return f"Shutdown request {req_id} sent to '{teammate}'"  # 返回发送结果


def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:  # 处理计划审查方法
    with _tracker_lock:  # 获取线程锁
        req = plan_requests.get(request_id)  # 获取计划请求
    if not req:  # 如果请求不存在
        return f"Error: Unknown plan request_id '{request_id}'"  # 返回未知请求错误
    with _tracker_lock:  # 获取线程锁
        req["status"] = "approved" if approve else "rejected"  # 更新请求状态
    BUS.send(  # 发送计划响应消息
        "lead", req["from"], feedback, "plan_approval_response",  # 发送给请求者
        {"request_id": request_id, "approve": approve, "feedback": feedback},  # 包含请求ID、批准状态和反馈
    )
    return f"Plan {req['status']} for '{req['from']}'"  # 返回处理结果


def _check_shutdown_status(request_id: str) -> str:  # 检查关闭状态方法
    with _tracker_lock:  # 获取线程锁
        return json.dumps(shutdown_requests.get(request_id, {"error": "not found"}))  # 返回关闭请求状态


# -- Lead tool dispatch (14 tools) --  # -- 领导者工具调度（14个工具） --
TOOL_HANDLERS = {  # 工具处理器字典
    "bash": lambda **kw: _run_bash(kw["command"]),  # bash命令处理器
    "read_file": lambda **kw: _run_read(kw["path"], kw.get("limit")),  # 读取文件处理器
    "write_file": lambda **kw: _run_write(kw["path"], kw["content"]),  # 写入文件处理器
    "edit_file": lambda **kw: _run_edit(kw["path"], kw["old_text"], kw["new_text"]),  # 编辑文件处理器
    "spawn_teammate": lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),  # 创建团队成员处理器
    "list_teammates": lambda **kw: TEAM.list_all(),  # 列出团队成员处理器
    "send_message": lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),  # 发送消息处理器
    "read_inbox": lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),  # 读取收件箱处理器
    "broadcast": lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),  # 广播消息处理器
    "shutdown_request": lambda **kw: handle_shutdown_request(kw["teammate"]),  # 关闭请求处理器
    "shutdown_response": lambda **kw: _check_shutdown_status(kw.get("request_id", "")),  # 关闭响应处理器
    "plan_approval": lambda **kw: handle_plan_review(kw["request_id"], kw["approve"], kw.get("feedback", "")),
    # 计划批准处理器
    "idle": lambda **kw: "Lead does not idle.",  # 空闲处理器
    "claim_task": lambda **kw: claim_task(kw["task_id"], "lead"),  # 认领任务处理器
}

# these base tools are unchanged from s02  # 这些基础工具与s02版本相同
TOOLS = [  # 工具定义列表
    {"name": "bash", "description": "Run a shell command.",  # bash命令工具
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    # 输入模式
    {"name": "read_file", "description": "Read file contents.",  # 读取文件工具
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
                      "required": ["path"]}},  # 输入模式
    {"name": "write_file", "description": "Write content to file.",  # 写入文件工具
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                      "required": ["path", "content"]}},  # 输入模式
    {"name": "edit_file", "description": "Replace exact text in file.",  # 编辑文件工具
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"},
                                                       "new_text": {"type": "string"}},
                      "required": ["path", "old_text", "new_text"]}},  # 输入模式
    {"name": "spawn_teammate", "description": "Spawn a persistent teammate.",  # 创建团队成员工具
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"},
                                                       "prompt": {"type": "string"}},
                      "required": ["name", "role", "prompt"]}},  # 输入模式
    {"name": "list_teammates", "description": "List all teammates.",  # 列出团队成员工具
     "input_schema": {"type": "object", "properties": {}}},  # 输入模式
    {"name": "send_message", "description": "Send a message to a teammate.",  # 发送消息工具
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"},
                                                       "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}},
                      "required": ["to", "content"]}},  # 输入模式
    {"name": "read_inbox", "description": "Read and drain the lead's inbox.",  # 读取领导者收件箱工具
     "input_schema": {"type": "object", "properties": {}}},  # 输入模式
    {"name": "broadcast", "description": "Send a message to all teammates.",  # 广播消息工具
     "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
    # 输入模式
    {"name": "shutdown_request",
     "description": "Request a teammate to shut down gracefully. Returns a request_id for tracking.",  # 关闭请求工具
     "input_schema": {"type": "object", "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]}},
    # 输入模式
    {"name": "shutdown_response", "description": "Check the status of a shutdown request by request_id.",  # 关闭响应工具
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
    # 输入模式
    {"name": "plan_approval",
     "description": "Approve or reject a teammate's plan. Provide request_id + approve + optional feedback.",  # 计划批准工具
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"},
                                                       "feedback": {"type": "string"}},
                      "required": ["request_id", "approve"]}},  # 输入模式
    {"name": "idle", "description": "Signal that you have no more work. Enters idle polling phase.",  # 空闲工具
     "input_schema": {"type": "object", "properties": {}}},  # 输入模式
    {"name": "claim_task", "description": "Claim a task from the task board by ID.",  # 认领任务工具
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
    # 输入模式
]


def agent_loop(messages: list):  # 代理主循环函数
    while True:  # 无限循环
        inbox = BUS.read_inbox("lead")  # 读取领导者收件箱
        if inbox:  # 如果收件箱不为空
            messages.append({  # 将收件箱内容添加到对话历史
                "role": "user",  # 用户角色
                "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>",  # 格式化的收件箱内容
            })
        response = client.messages.create(  # 创建消息请求
            model=MODEL,  # 使用配置的模型
            system=SYSTEM,  # 系统提示
            messages=messages,  # 对话历史
            tools=TOOLS,  # 可用工具
            max_tokens=8000,  # 最大token数
        )
        messages.append({"role": "assistant", "content": response.content})  # 将AI响应添加到对话历史
        if response.stop_reason != "tool_use":  # 如果停止原因不是工具使用
            return  # 返回结束循环
        results = []  # 初始化结果列表
        for block in response.content:  # 遍历响应内容块
            if block.type == "tool_use":  # 如果是工具使用块
                handler = TOOL_HANDLERS.get(block.name)  # 获取对应的处理器
                try:  # 尝试执行工具
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"  # 执行工具或返回未知工具错误
                except Exception as e:  # 如果发生异常
                    output = f"Error: {e}"  # 返回错误信息
                print(f"> {block.name}:")  # 打印工具名称
                print(str(output)[:200])  # 打印工具输出（截断）
                results.append({  # 构建工具结果
                    "type": "tool_result",  # 结果类型
                    "tool_use_id": block.id,  # 工具使用ID
                    "content": str(output),  # 结果内容
                })
        messages.append({"role": "user", "content": results})  # 将工具结果添加到对话历史


if __name__ == "__main__":  # 主程序入口
    history = []  # 初始化对话历史
    while True:  # 无限循环
        try:  # 尝试获取用户输入
            query = input("\033[36ms11 >> \033[0m")  # 显示提示符并获取用户输入
        except (EOFError, KeyboardInterrupt):  # 如果捕获到EOF或键盘中断
            break  # 退出循环
        if query.strip().lower() in ("q", "exit", ""):  # 如果输入是退出命令
            break  # 退出循环
        if query.strip() == "/team":  # 如果输入是/team命令
            print(TEAM.list_all())  # 打印团队成员列表
            continue  # 继续下一次循环
        if query.strip() == "/inbox":  # 如果输入是/inbox命令
            print(json.dumps(BUS.read_inbox("lead"), indent=2))  # 打印领导者收件箱
            continue  # 继续下一次循环
        history.append({"role": "user", "content": query})  # 将用户输入添加到对话历史
        agent_loop(history)  # 执行代理主循环
        response_content = history[-1]["content"]  # 获取最后一条响应内容
        if isinstance(response_content, list):  # 如果响应内容是列表
            for block in response_content:  # 遍历响应块
                if hasattr(block, "text"):  # 如果有text属性
                    print(block.text)  # 打印文本内容
        print()  # 打印空行
