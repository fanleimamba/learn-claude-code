#!/usr/bin/env python3  # Python解释器路径声明，确保脚本使用Python3执行
# Harness: directory isolation -- parallel execution lanes that never collide.  # 目录隔离框架：并行执行通道永不冲突
"""
s12_worktree_task_isolation.py - Worktree + Task Isolation  # 工作树+任务隔离模块

Directory-level isolation for parallel task execution.  # 用于并行任务执行的目录级隔离
Tasks are the control plane and worktrees are the execution plane.  # 任务是控制平面，工作树是执行平面

    .tasks/task_12.json  # 任务文件示例
      {  # 任务JSON结构
        "id": 12,  # 任务ID
        "subject": "Implement auth refactor",  # 任务主题
        "status": "in_progress",  # 任务状态
        "worktree": "auth-refactor"  # 关联的工作树名称
      }

    .worktrees/index.json  # 工作树索引文件
      {  # 索引JSON结构
        "worktrees": [  # 工作树列表
          {  # 单个工作树信息
            "name": "auth-refactor",  # 工作树名称
            "path": ".../.worktrees/auth-refactor",  # 工作树路径
            "branch": "wt/auth-refactor",  # 分支名称
            "task_id": 12,  # 关联的任务ID
            "status": "active"  # 工作树状态
          }
        ]
      }

Key insight: "Isolate by directory, coordinate by task ID."  # 关键洞察："按目录隔离，按任务ID协调"
"""  # 文档字符串结束

import json  # 导入JSON处理模块
import os  # 导入操作系统接口模块
import re  # 导入正则表达式模块
import subprocess  # 导入子进程管理模块
import time  # 导入时间处理模块
from pathlib import Path  # 从pathlib导入Path类，用于路径操作

from anthropic import Anthropic  # 从anthropic导入Anthropic类，用于AI客户端
from dotenv import load_dotenv  # 从dotenv导入load_dotenv，用于加载环境变量

load_dotenv(override=True)  # 加载环境变量，覆盖现有的环境变量

if os.getenv("ANTHROPIC_BASE_URL"):  # 如果设置了ANTHROPIC_BASE_URL环境变量
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)  # 移除ANTHROPIC_AUTH_TOKEN环境变量

WORKDIR = Path.cwd()  # 设置工作目录为当前工作目录
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # 创建Anthropic客户端实例，使用环境变量中的基础URL
MODEL = os.environ["MODEL_ID"]  # 从环境变量获取模型ID


def detect_repo_root(cwd: Path) -> Path | None:  # 检测仓库根目录函数
    """Return git repo root if cwd is inside a repo, else None."""  # 如果当前目录在git仓库中，返回仓库根目录，否则返回None
    try:  # 尝试执行
        r = subprocess.run(  # 运行子进程
            ["git", "rev-parse", "--show-toplevel"],  # git命令：显示顶级目录
            cwd=cwd,  # 设置工作目录
            capture_output=True,  # 捕获输出
            text=True,  # 文本模式
            timeout=10,  # 超时10秒
        )
        if r.returncode != 0:  # 如果返回码不为0
            return None  # 返回None
        root = Path(r.stdout.strip())  # 解析输出为路径
        return root if root.exists() else None  # 如果路径存在则返回，否则返回None
    except Exception:  # 如果发生异常
        return None  # 返回None


REPO_ROOT = detect_repo_root(WORKDIR) or WORKDIR  # 仓库根目录：检测到的仓库根目录或当前工作目录

SYSTEM = (  # 系统提示语
    f"You are a coding agent at {WORKDIR}. "  # 你是位于{WORKDIR}的编码代理
    "Use task + worktree tools for multi-task work. "  # 使用任务和工作树工具进行多任务工作
    "For parallel or risky changes: create tasks, allocate worktree lanes, "  # 对于并行或风险更改：创建任务，分配工作树通道
    "run commands in those lanes, then choose keep/remove for closeout. "  # 在这些通道中运行命令，然后选择保留/删除进行收尾
    "Use worktree_events when you need lifecycle visibility."  # 当需要生命周期可见性时使用worktree_events
)


# -- EventBus: append-only lifecycle events for observability --  # -- 事件总线：仅追加的生命周期事件用于可观察性 --
class EventBus:  # 定义事件总线类
    def __init__(self, event_log_path: Path):  # 初始化方法，接收事件日志路径
        self.path = event_log_path  # 存储事件日志路径
        self.path.parent.mkdir(parents=True, exist_ok=True)  # 创建父目录（如果不存在）
        if not self.path.exists():  # 如果事件日志文件不存在
            self.path.write_text("")  # 创建空文件

    def emit(  # 发送事件方法
        self,
        event: str,  # 事件名称
        task: dict | None = None,  # 任务信息（可选）
        worktree: dict | None = None,  # 工作树信息（可选）
        error: str | None = None,  # 错误信息（可选）
    ):
        payload = {  # 构建事件负载
            "event": event,  # 事件名称
            "ts": time.time(),  # 时间戳
            "task": task or {},  # 任务信息（默认为空字典）
            "worktree": worktree or {},  # 工作树信息（默认为空字典）
        }
        if error:  # 如果有错误信息
            payload["error"] = error  # 添加错误信息
        with self.path.open("a", encoding="utf-8") as f:  # 以追加模式打开文件
            f.write(json.dumps(payload) + "\n")  # 将事件序列化为JSON并写入文件，添加换行符

    def list_recent(self, limit: int = 20) -> str:  # 列出最近事件方法
        n = max(1, min(int(limit or 20), 200))  # 限制事件数量在1-200之间
        lines = self.path.read_text(encoding="utf-8").splitlines()  # 读取文件内容并按行分割
        recent = lines[-n:]  # 获取最后n行
        items = []  # 初始化事件列表
        for line in recent:  # 遍历每行
            try:  # 尝试解析
                items.append(json.loads(line))  # 解析JSON行并添加到列表
            except Exception:  # 如果解析失败
                items.append({"event": "parse_error", "raw": line})  # 添加解析错误事件
        return json.dumps(items, indent=2)  # 返回格式化的JSON字符串


# -- TaskManager: persistent task board with optional worktree binding --  # -- 任务管理器：具有可选工作树绑定的持久任务板 --
class TaskManager:  # 定义任务管理器类
    def __init__(self, tasks_dir: Path):  # 初始化方法，接收任务目录路径
        self.dir = tasks_dir  # 存储任务目录路径
        self.dir.mkdir(parents=True, exist_ok=True)  # 创建任务目录（如果不存在）
        self._next_id = self._max_id() + 1  # 设置下一个任务ID为最大ID+1

    def _max_id(self) -> int:  # 获取最大任务ID方法
        ids = []  # 初始化ID列表
        for f in self.dir.glob("task_*.json"):  # 遍历所有任务文件
            try:  # 尝试提取ID
                ids.append(int(f.stem.split("_")[1]))  # 从文件名提取ID
            except Exception:  # 如果提取失败
                pass  # 忽略
        return max(ids) if ids else 0  # 返回最大ID或0

    def _path(self, task_id: int) -> Path:  # 获取任务文件路径方法
        return self.dir / f"task_{task_id}.json"  # 构建任务文件路径

    def _load(self, task_id: int) -> dict:  # 加载任务方法
        path = self._path(task_id)  # 获取任务文件路径
        if not path.exists():  # 如果任务文件不存在
            raise ValueError(f"Task {task_id} not found")  # 抛出未找到错误
        return json.loads(path.read_text())  # 读取并解析任务文件

    def _save(self, task: dict):  # 保存任务方法
        self._path(task["id"]).write_text(json.dumps(task, indent=2))  # 将任务序列化为JSON并写入文件

    def create(self, subject: str, description: str = "") -> str:  # 创建任务方法
        task = {  # 构建任务字典
            "id": self._next_id,  # 任务ID
            "subject": subject,  # 任务主题
            "description": description,  # 任务描述
            "status": "pending",  # 任务状态
            "owner": "",  # 任务所有者
            "worktree": "",  # 关联工作树
            "blockedBy": [],  # 被阻塞的任务列表
            "created_at": time.time(),  # 创建时间
            "updated_at": time.time(),  # 更新时间
        }
        self._save(task)  # 保存任务
        self._next_id += 1  # 递增下一个任务ID
        return json.dumps(task, indent=2)  # 返回格式化的任务JSON

    def get(self, task_id: int) -> str:  # 获取任务方法
        return json.dumps(self._load(task_id), indent=2)  # 返回格式化的任务JSON

    def exists(self, task_id: int) -> bool:  # 检查任务是否存在方法
        return self._path(task_id).exists()  # 返回任务文件是否存在

    def update(self, task_id: int, status: str = None, owner: str = None) -> str:  # 更新任务方法
        task = self._load(task_id)  # 加载任务
        if status:  # 如果提供了状态
            if status not in ("pending", "in_progress", "completed"):  # 如果状态无效
                raise ValueError(f"Invalid status: {status}")  # 抛出无效状态错误
            task["status"] = status  # 更新任务状态
        if owner is not None:  # 如果提供了所有者
            task["owner"] = owner  # 更新任务所有者
        task["updated_at"] = time.time()  # 更新更新时间
        self._save(task)  # 保存任务
        return json.dumps(task, indent=2)  # 返回格式化的任务JSON

    def bind_worktree(self, task_id: int, worktree: str, owner: str = "") -> str:  # 绑定工作树方法
        task = self._load(task_id)  # 加载任务
        task["worktree"] = worktree  # 设置关联工作树
        if owner:  # 如果提供了所有者
            task["owner"] = owner  # 更新任务所有者
        if task["status"] == "pending":  # 如果任务状态为待处理
            task["status"] = "in_progress"  # 更新状态为进行中
        task["updated_at"] = time.time()  # 更新时间
        self._save(task)  # 保存任务
        return json.dumps(task, indent=2)  # 返回格式化的任务JSON

    def unbind_worktree(self, task_id: int) -> str:  # 解绑工作树方法
        task = self._load(task_id)  # 加载任务
        task["worktree"] = ""  # 清空关联工作树
        task["updated_at"] = time.time()  # 更新时间
        self._save(task)  # 保存任务
        return json.dumps(task, indent=2)  # 返回格式化的任务JSON

    def list_all(self) -> str:  # 列出所有任务方法
        tasks = []  # 初始化任务列表
        for f in sorted(self.dir.glob("task_*.json")):  # 遍历所有任务文件
            tasks.append(json.loads(f.read_text()))  # 读取并解析任务文件
        if not tasks:  # 如果没有任务
            return "No tasks."  # 返回无任务信息
        lines = []  # 初始化输出行列表
        for t in tasks:  # 遍历所有任务
            marker = {  # 状态标记映射
                "pending": "[ ]",  # 待处理
                "in_progress": "[>]",  # 进行中
                "completed": "[x]",  # 已完成
            }.get(t["status"], "[?]")  # 获取标记或默认标记
            owner = f" owner={t['owner']}" if t.get("owner") else ""  # 所有者信息
            wt = f" wt={t['worktree']}" if t.get("worktree") else ""  # 工作树信息
            lines.append(f"{marker} #{t['id']}: {t['subject']}{owner}{wt}")  # 构建任务行
        return "\n".join(lines)  # 连接所有行并返回


TASKS = TaskManager(REPO_ROOT / ".tasks")  # 创建全局任务管理器实例
EVENTS = EventBus(REPO_ROOT / ".worktrees" / "events.jsonl")  # 创建全局事件总线实例


# -- WorktreeManager: create/list/run/remove git worktrees + lifecycle index --  # -- 工作树管理器：创建/列出/运行/删除git工作树+生命周期索引 --
class WorktreeManager:  # 定义工作树管理器类
    def __init__(self, repo_root: Path, tasks: TaskManager, events: EventBus):  # 初始化方法
        self.repo_root = repo_root  # 存储仓库根目录
        self.tasks = tasks  # 存储任务管理器
        self.events = events  # 存储事件总线
        self.dir = repo_root / ".worktrees"  # 工作树目录路径
        self.dir.mkdir(parents=True, exist_ok=True)  # 创建工作树目录（如果不存在）
        self.index_path = self.dir / "index.json"  # 索引文件路径
        if not self.index_path.exists():  # 如果索引文件不存在
            self.index_path.write_text(json.dumps({"worktrees": []}, indent=2))  # 创建空索引文件
        self.git_available = self._is_git_repo()  # 检查git是否可用

    def _is_git_repo(self) -> bool:  # 检查是否为git仓库方法
        try:  # 尝试执行
            r = subprocess.run(  # 运行子进程
                ["git", "rev-parse", "--is-inside-work-tree"],  # git命令：检查是否在git工作树中
                cwd=self.repo_root,  # 设置工作目录
                capture_output=True,  # 捕获输出
                text=True,  # 文本模式
                timeout=10,  # 超时10秒
            )
            return r.returncode == 0  # 返回是否成功
        except Exception:  # 如果发生异常
            return False  # 返回False

    def _run_git(self, args: list[str]) -> str:  # 运行git命令方法
        if not self.git_available:  # 如果git不可用
            raise RuntimeError("Not in a git repository. worktree tools require git.")  # 抛出错误
        r = subprocess.run(  # 运行子进程
            ["git", *args],  # git命令和参数
            cwd=self.repo_root,  # 设置工作目录
            capture_output=True,  # 捕获输出
            text=True,  # 文本模式
            timeout=120,  # 超时120秒
        )
        if r.returncode != 0:  # 如果返回码不为0
            msg = (r.stdout + r.stderr).strip()  # 获取错误信息
            raise RuntimeError(msg or f"git {' '.join(args)} failed")  # 抛出运行时错误
        return (r.stdout + r.stderr).strip() or "(no output)"  # 返回输出或默认信息

    def _load_index(self) -> dict:  # 加载索引方法
        return json.loads(self.index_path.read_text())  # 读取并解析索引文件

    def _save_index(self, data: dict):  # 保存索引方法
        self.index_path.write_text(json.dumps(data, indent=2))  # 将数据序列化为JSON并写入文件

    def _find(self, name: str) -> dict | None:  # 查找工作树方法
        idx = self._load_index()  # 加载索引
        for wt in idx.get("worktrees", []):  # 遍历工作树列表
            if wt.get("name") == name:  # 如果找到匹配的工作树
                return wt  # 返回工作树信息
        return None  # 未找到返回None

    def _validate_name(self, name: str):  # 验证工作树名称方法
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", name or ""):  # 如果名称不符合规范
            raise ValueError(  # 抛出值错误
                "Invalid worktree name. Use 1-40 chars: letters, numbers, ., _, -"  # 错误信息
            )

    def create(self, name: str, task_id: int = None, base_ref: str = "HEAD") -> str:  # 创建工作树方法
        self._validate_name(name)  # 验证工作树名称
        if self._find(name):  # 如果工作树已存在
            raise ValueError(f"Worktree '{name}' already exists in index")  # 抛出已存在错误
        if task_id is not None and not self.tasks.exists(task_id):  # 如果任务ID提供但任务不存在
            raise ValueError(f"Task {task_id} not found")  # 抛出未找到错误

        path = self.dir / name  # 工作树路径
        branch = f"wt/{name}"  # 分支名称
        self.events.emit(  # 发送事件
            "worktree.create.before",  # 事件名称
            task={"id": task_id} if task_id is not None else {},  # 任务信息
            worktree={"name": name, "base_ref": base_ref},  # 工作树信息
        )
        try:  # 尝试执行
            self._run_git(["worktree", "add", "-b", branch, str(path), base_ref])  # 运行git工作树添加命令

            entry = {  # 构建工作树条目
                "name": name,  # 名称
                "path": str(path),  # 路径
                "branch": branch,  # 分支
                "task_id": task_id,  # 任务ID
                "status": "active",  # 状态
                "created_at": time.time(),  # 创建时间
            }

            idx = self._load_index()  # 加载索引
            idx["worktrees"].append(entry)  # 添加工作树条目
            self._save_index(idx)  # 保存索引

            if task_id is not None:  # 如果提供了任务ID
                self.tasks.bind_worktree(task_id, name)  # 绑定任务到工作树

            self.events.emit(  # 发送事件
                "worktree.create.after",  # 事件名称
                task={"id": task_id} if task_id is not None else {},  # 任务信息
                worktree={  # 工作树信息
                    "name": name,  # 名称
                    "path": str(path),  # 路径
                    "branch": branch,  # 分支
                    "status": "active",  # 状态
                },
            )
            return json.dumps(entry, indent=2)  # 返回格式化的JSON
        except Exception as e:  # 如果发生异常
            self.events.emit(  # 发送事件
                "worktree.create.failed",  # 事件名称
                task={"id": task_id} if task_id is not None else {},  # 任务信息
                worktree={"name": name, "base_ref": base_ref},  # 工作树信息
                error=str(e),  # 错误信息
            )
            raise  # 重新抛出异常

    def list_all(self) -> str:  # 列出所有工作树方法
        idx = self._load_index()  # 加载索引
        wts = idx.get("worktrees", [])  # 获取工作树列表
        if not wts:  # 如果没有工作树
            return "No worktrees in index."  # 返回无工作树信息
        lines = []  # 初始化输出行列表
        for wt in wts:  # 遍历所有工作树
            suffix = f" task={wt['task_id']}" if wt.get("task_id") else ""  # 任务ID后缀
            lines.append(  # 添加工作树行
                f"[{wt.get('status', 'unknown')}] {wt['name']} -> "  # 状态和名称
                f"{wt['path']} ({wt.get('branch', '-')}){suffix}"  # 路径和分支
            )
        return "\n".join(lines)  # 连接所有行并返回

    def status(self, name: str) -> str:  # 获取工作树状态方法
        wt = self._find(name)  # 查找工作树
        if not wt:  # 如果未找到
            return f"Error: Unknown worktree '{name}'"  # 返回错误信息
        path = Path(wt["path"])  # 工作树路径
        if not path.exists():  # 如果路径不存在
            return f"Error: Worktree path missing: {path}"  # 返回路径缺失错误
        r = subprocess.run(  # 运行子进程
            ["git", "status", "--short", "--branch"],  # git状态命令
            cwd=path,  # 设置工作目录
            capture_output=True,  # 捕获输出
            text=True,  # 文本模式
            timeout=60,  # 超时60秒
        )
        text = (r.stdout + r.stderr).strip()  # 获取输出文本
        return text or "Clean worktree"  # 返回文本或默认信息

    def run(self, name: str, command: str) -> str:  # 在工作树中运行命令方法
        dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]  # 危险命令列表
        if any(d in command for d in dangerous):  # 如果命令包含危险指令
            return "Error: Dangerous command blocked"  # 返回危险命令被阻止错误

        wt = self._find(name)  # 查找工作树
        if not wt:  # 如果未找到
            return f"Error: Unknown worktree '{name}'"  # 返回错误信息
        path = Path(wt["path"])  # 工作树路径
        if not path.exists():  # 如果路径不存在
            return f"Error: Worktree path missing: {path}"  # 返回路径缺失错误

        try:  # 尝试执行
            r = subprocess.run(  # 运行子进程
                command,  # 命令
                shell=True,  # 使用shell
                cwd=path,  # 设置工作目录
                capture_output=True,  # 捕获输出
                text=True,  # 文本模式
                timeout=300,  # 超时300秒
            )
            out = (r.stdout + r.stderr).strip()  # 获取输出
            return out[:50000] if out else "(no output)"  # 返回输出（截断）或无输出提示
        except subprocess.TimeoutExpired:  # 如果超时
            return "Error: Timeout (300s)"  # 返回超时错误

    def remove(self, name: str, force: bool = False, complete_task: bool = False) -> str:  # 移除工作树方法
        wt = self._find(name)  # 查找工作树
        if not wt:  # 如果未找到
            return f"Error: Unknown worktree '{name}'"  # 返回错误信息

        self.events.emit(  # 发送事件
            "worktree.remove.before",  # 事件名称
            task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},  # 任务信息
            worktree={"name": name, "path": wt.get("path")},  # 工作树信息
        )
        try:  # 尝试执行
            args = ["worktree", "remove"]  # git命令参数
            if force:  # 如果强制删除
                args.append("--force")  # 添加强制参数
            args.append(wt["path"])  # 添加工作树路径
            self._run_git(args)  # 运行git命令

            if complete_task and wt.get("task_id") is not None:  # 如果要求完成任务且有关联任务
                task_id = wt["task_id"]  # 获取任务ID
                before = json.loads(self.tasks.get(task_id))  # 获取任务信息
                self.tasks.update(task_id, status="completed")  # 更新任务状态为已完成
                self.tasks.unbind_worktree(task_id)  # 解绑工作树
                self.events.emit(  # 发送事件
                    "task.completed",  # 事件名称
                    task={  # 任务信息
                        "id": task_id,  # 任务ID
                        "subject": before.get("subject", ""),  # 任务主题
                        "status": "completed",  # 任务状态
                    },
                    worktree={"name": name},  # 工作树信息
                )

            idx = self._load_index()  # 加载索引
            for item in idx.get("worktrees", []):  # 遍历工作树列表
                if item.get("name") == name:  # 如果找到匹配的工作树
                    item["status"] = "removed"  # 更新状态为已删除
                    item["removed_at"] = time.time()  # 设置删除时间
            self._save_index(idx)  # 保存索引

            self.events.emit(  # 发送事件
                "worktree.remove.after",  # 事件名称
                task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},  # 任务信息
                worktree={  # 工作树信息
                    "name": name,  # 名称
                    "path": wt.get("path"),  # 路径
                    "status": "removed",  # 状态
                },
            )
            return f"Removed worktree '{name}'"  # 返回删除成功信息
        except Exception as e:  # 如果发生异常
            self.events.emit(  # 发送事件
                "worktree.remove.failed",  # 事件名称
                task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},  # 任务信息
                worktree={"name": name, "path": wt.get("path")},  # 工作树信息
                error=str(e),  # 错误信息
            )
            raise  # 重新抛出异常

    def keep(self, name: str) -> str:  # 保留工作树方法
        wt = self._find(name)  # 查找工作树
        if not wt:  # 如果未找到
            return f"Error: Unknown worktree '{name}'"  # 返回错误信息

        idx = self._load_index()  # 加载索引
        kept = None  # 初始化保留的工作树
        for item in idx.get("worktrees", []):  # 遍历工作树列表
            if item.get("name") == name:  # 如果找到匹配的工作树
                item["status"] = "kept"  # 更新状态为已保留
                item["kept_at"] = time.time()  # 设置保留时间
                kept = item  # 存储保留的工作树
        self._save_index(idx)  # 保存索引

        self.events.emit(  # 发送事件
            "worktree.keep",  # 事件名称
            task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},  # 任务信息
            worktree={  # 工作树信息
                "name": name,  # 名称
                "path": wt.get("path"),  # 路径
                "status": "kept",  # 状态
            },
        )
        return json.dumps(kept, indent=2) if kept else f"Error: Unknown worktree '{name}'"  # 返回格式化的JSON或错误信息


WORKTREES = WorktreeManager(REPO_ROOT, TASKS, EVENTS)  # 创建全局工作树管理器实例


# -- Base tools (kept minimal, same style as previous sessions) --  # -- 基础工具（保持最小化，与之前会话风格相同） --
def safe_path(p: str) -> Path:  # 安全路径处理函数
    path = (WORKDIR / p).resolve()  # 解析相对路径为绝对路径
    if not path.is_relative_to(WORKDIR):  # 如果路径不在工作目录内
        raise ValueError(f"Path escapes workspace: {p}")  # 抛出路径越界错误
    return path  # 返回安全路径


def run_bash(command: str) -> str:  # 运行bash命令函数
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]  # 危险命令列表
    if any(d in command for d in dangerous):  # 如果命令包含危险指令
        return "Error: Dangerous command blocked"  # 返回危险命令被阻止错误
    try:  # 尝试执行
        r = subprocess.run(  # 运行子进程
            command,  # 命令
            shell=True,  # 使用shell
            cwd=WORKDIR,  # 设置工作目录
            capture_output=True,  # 捕获输出
            text=True,  # 文本模式
            timeout=120,  # 超时120秒
        )
        out = (r.stdout + r.stderr).strip()  # 获取输出
        return out[:50000] if out else "(no output)"  # 返回输出（截断）或无输出提示
    except subprocess.TimeoutExpired:  # 如果超时
        return "Error: Timeout (120s)"  # 返回超时错误


def run_read(path: str, limit: int = None) -> str:  # 读取文件函数
    try:  # 尝试读取
        lines = safe_path(path).read_text().splitlines()  # 安全读取文件并按行分割
        if limit and limit < len(lines):  # 如果设置了限制且行数超过限制
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]  # 截取前limit行并添加提示
        return "\n".join(lines)[:50000]  # 连接行并返回（截断）
    except Exception as e:  # 如果发生异常
        return f"Error: {e}"  # 返回错误信息


def run_write(path: str, content: str) -> str:  # 写入文件函数
    try:  # 尝试写入
        fp = safe_path(path)  # 获取安全路径
        fp.parent.mkdir(parents=True, exist_ok=True)  # 创建父目录（如果不存在）
        fp.write_text(content)  # 写入内容
        return f"Wrote {len(content)} bytes"  # 返回写入字节数
    except Exception as e:  # 如果发生异常
        return f"Error: {e}"  # 返回错误信息


def run_edit(path: str, old_text: str, new_text: str) -> str:  # 编辑文件函数
    try:  # 尝试编辑
        fp = safe_path(path)  # 获取安全路径
        c = fp.read_text()  # 读取文件内容
        if old_text not in c:  # 如果要替换的文本不存在
            return f"Error: Text not found in {path}"  # 返回未找到文本错误
        fp.write_text(c.replace(old_text, new_text, 1))  # 替换文本并写入
        return f"Edited {path}"  # 返回编辑成功信息
    except Exception as e:  # 如果发生异常
        return f"Error: {e}"  # 返回错误信息


TOOL_HANDLERS = {  # 工具处理器字典
    "bash": lambda **kw: run_bash(kw["command"]),  # bash命令处理器
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),  # 读取文件处理器
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),  # 写入文件处理器
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),  # 编辑文件处理器
    "task_create": lambda **kw: TASKS.create(kw["subject"], kw.get("description", "")),  # 创建任务处理器
    "task_list": lambda **kw: TASKS.list_all(),  # 列出任务处理器
    "task_get": lambda **kw: TASKS.get(kw["task_id"]),  # 获取任务处理器
    "task_update": lambda **kw: TASKS.update(kw["task_id"], kw.get("status"), kw.get("owner")),  # 更新任务处理器
    "task_bind_worktree": lambda **kw: TASKS.bind_worktree(kw["task_id"], kw["worktree"], kw.get("owner", "")),  # 绑定工作树处理器
    "worktree_create": lambda **kw: WORKTREES.create(kw["name"], kw.get("task_id"), kw.get("base_ref", "HEAD")),  # 创建工作树处理器
    "worktree_list": lambda **kw: WORKTREES.list_all(),  # 列出工作树处理器
    "worktree_status": lambda **kw: WORKTREES.status(kw["name"]),  # 工作树状态处理器
    "worktree_run": lambda **kw: WORKTREES.run(kw["name"], kw["command"]),  # 工作树运行命令处理器
    "worktree_keep": lambda **kw: WORKTREES.keep(kw["name"]),  # 保留工作树处理器
    "worktree_remove": lambda **kw: WORKTREES.remove(kw["name"], kw.get("force", False), kw.get("complete_task", False)),  # 移除工作树处理器
    "worktree_events": lambda **kw: EVENTS.list_recent(kw.get("limit", 20)),  # 工作树事件处理器
}

TOOLS = [  # 工具定义列表
    {
        "name": "bash",  # bash命令工具
        "description": "Run a shell command in the current workspace (blocking).",  # 描述
        "input_schema": {  # 输入模式
            "type": "object",  # 对象类型
            "properties": {  # 属性
                "command": {"type": "string"}  # 命令属性
            },
            "required": ["command"],  # 必需属性
        },
    },
    {
        "name": "read_file",  # 读取文件工具
        "description": "Read file contents.",  # 描述
        "input_schema": {  # 输入模式
            "type": "object",  # 对象类型
            "properties": {  # 属性
                "path": {"type": "string"},  # 路径属性
                "limit": {"type": "integer"},  # 限制属性
            },
            "required": ["path"],  # 必需属性
        },
    },
    {
        "name": "write_file",  # 写入文件工具
        "description": "Write content to file.",  # 描述
        "input_schema": {  # 输入模式
            "type": "object",  # 对象类型
            "properties": {  # 属性
                "path": {"type": "string"},  # 路径属性
                "content": {"type": "string"},  # 内容属性
            },
            "required": ["path", "content"],  # 必需属性
        },
    },
    {
        "name": "edit_file",  # 编辑文件工具
        "description": "Replace exact text in file.",  # 描述
        "input_schema": {  # 输入模式
            "type": "object",  # 对象类型
            "properties": {  # 属性
                "path": {"type": "string"},  # 路径属性
                "old_text": {"type": "string"},  # 旧文本属性
                "new_text": {"type": "string"},  # 新文本属性
            },
            "required": ["path", "old_text", "new_text"],  # 必需属性
        },
    },
    {
        "name": "task_create",  # 创建任务工具
        "description": "Create a new task with subject and optional description.",  # 描述
        "input_schema": {  # 输入模式
            "type": "object",  # 对象类型
            "properties": {  # 属性
                "subject": {"type": "string"},  # 主题属性
                "description": {"type": "string"},  # 描述属性
            },
            "required": ["subject"],  # 必需属性
        },
    },
    {
        "name": "task_list",  # 列出任务工具
        "description": "List all tasks with status markers.",  # 描述
        "input_schema": {  # 输入模式
            "type": "object",  # 对象类型
            "properties": {},  # 无属性
        },
    },
    {
        "name": "task_get",  # 获取任务工具
        "description": "Get full details of a task by ID.",  # 描述
        "input_schema": {  # 输入模式
            "type": "object",  # 对象类型
            "properties": {  # 属性
                "task_id": {"type": "integer"},  # 任务ID属性
            },
            "required": ["task_id"],  # 必需属性
        },
    },
    {
        "name": "task_update",  # 更新任务工具
        "description": "Update task status and/or owner.",  # 描述
        "input_schema": {  # 输入模式
            "type": "object",  # 对象类型
            "properties": {  # 属性
                "task_id": {"type": "integer"},  # 任务ID属性
                "status": {"type": "string"},  # 状态属性
                "owner": {"type": "string"},  # 所有者属性
            },
            "required": ["task_id"],  # 必需属性
        },
    },
    {
        "name": "task_bind_worktree",  # 绑定工作树工具
        "description": "Bind a task to a specific worktree.",  # 描述
        "input_schema": {  # 输入模式
            "type": "object",  # 对象类型
            "properties": {  # 属性
                "task_id": {"type": "integer"},  # 任务ID属性
                "worktree": {"type": "string"},  # 工作树属性
                "owner": {"type": "string"},  # 所有者属性
            },
            "required": ["task_id", "worktree"],  # 必需属性
        },
    },
    {
        "name": "worktree_create",  # 创建工作树工具
        "description": "Create a new git worktree for isolated development.",  # 描述
        "input_schema": {  # 输入模式
            "type": "object",  # 对象类型
            "properties": {  # 属性
                "name": {"type": "string"},  # 名称属性
                "task_id": {"type": "integer"},  # 任务ID属性
                "base_ref": {"type": "string"},  # 基础引用属性
            },
            "required": ["name"],  # 必需属性
        },
    },
    {
        "name": "worktree_list",  # 列出工作树工具
        "description": "List all managed worktrees with status.",  # 描述
        "input_schema": {  # 输入模式
            "type": "object",  # 对象类型
            "properties": {},  # 无属性
        },
    },
    {
        "name": "worktree_status",  # 工作树状态工具
        "description": "Show git status for a worktree.",  # 描述
        "input_schema": {  # 输入模式
            "type": "object",  # 对象类型
            "properties": {  # 属性
                "name": {"type": "string"},  # 名称属性
            },
            "required": ["name"],  # 必需属性
        },
    },
    {
        "name": "worktree_run",  # 工作树运行命令工具
        "description": "Run a shell command inside a specific worktree.",  # 描述
        "input_schema": {  # 输入模式
            "type": "object",  # 对象类型
            "properties": {  # 属性
                "name": {"type": "string"},  # 名称属性
                "command": {"type": "string"},  # 命令属性
            },
            "required": ["name", "command"],  # 必需属性
        },
    },
    {
        "name": "worktree_keep",  # 保留工作树工具
        "description": "Mark a worktree as kept (won't be removed automatically).",  # 描述
        "input_schema": {  # 输入模式
            "type": "object",  # 对象类型
            "properties": {  # 属性
                "name": {"type": "string"},  # 名称属性
            },
            "required": ["name"],  # 必需属性
        },
    },
    {
        "name": "worktree_remove",  # 移除工作树工具
        "description": "Remove a worktree and optionally mark its task as completed.",  # 描述
        "input_schema": {  # 输入模式
            "type": "object",  # 对象类型
            "properties": {  # 属性
                "name": {"type": "string"},  # 名称属性
                "force": {"type": "boolean"},  # 强制属性
                "complete_task": {"type": "boolean"},  # 完成任务属性
            },
            "required": ["name"],  # 必需属性
        },
    },
    {
        "name": "worktree_events",  # 工作树事件工具
        "description": "List recent worktree lifecycle events.",  # 描述
        "input_schema": {  # 输入模式
            "type": "object",  # 对象类型
            "properties": {  # 属性
                "limit": {"type": "integer"},  # 限制属性
            },
        },
    },
]