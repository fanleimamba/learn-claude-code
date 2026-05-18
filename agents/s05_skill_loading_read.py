#!/usr/bin/env python3  # shebang行，指定使用python3解释器执行此脚本
# Harness: on-demand knowledge -- domain expertise, loaded when the model asks.  # 注释：按需知识 - 当模型请求时加载领域专业知识
"""
s05_skill_loading.py - Skills  # 文档字符串：文件名和简要描述

Two-layer skill injection that avoids bloating the system prompt:  # 双层技能注入，避免系统提示膨胀

    Layer 1 (cheap): skill names in system prompt (~100 tokens/skill)  # 第一层（低成本）：在系统提示中只包含技能名称（每个技能约100token）
    Layer 2 (on demand): full skill body in tool_result  # 第二层（按需）：在工具结果中返回完整技能内容

    skills/  # 技能目录结构
      pdf/
        SKILL.md          <-- frontmatter (name, description) + body  # 技能文件，包含元数据和主体内容
      code-review/
        SKILL.md

    System prompt:  # 系统提示示例
    +--------------------------------------+
    | You are a coding agent.              |
    | Skills available:                    |
    |   - pdf: Process PDF files...        |  <-- Layer 1: metadata only  # 第一层：只包含元数据
    |   - code-review: Review code...      |
    +--------------------------------------+

    When model calls load_skill("pdf"):  # 当模型调用load_skill("pdf")时
    +--------------------------------------+
    | tool_result:                         |
    | <skill>                              |
    |   Full PDF processing instructions   |  <-- Layer 2: full body  # 第二层：完整内容
    |   Step 1: ...                        |
    |   Step 2: ...                        |
    | </skill>                             |
    +--------------------------------------+

Key insight: "Don't put everything in the system prompt. Load on demand."  # 关键洞察：不要把所有内容都放在系统提示中，按需加载
"""

import os  # 导入操作系统接口模块
import re  # 导入正则表达式模块
import subprocess  # 导入子进程管理模块
import yaml  # 导入YAML解析模块
from pathlib import Path  # 从pathlib导入Path类，用于路径操作

from anthropic import Anthropic  # 从anthropic库导入Anthropic类
from dotenv import load_dotenv  # 从dotenv库导入load_dotenv函数

load_dotenv(override=True)  # 加载环境变量，override=True表示覆盖已存在的变量

if os.getenv("ANTHROPIC_BASE_URL"):  # 如果设置了ANTHROPIC_BASE_URL环境变量
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)  # 则移除ANTHROPIC_AUTH_TOKEN环境变量

WORKDIR = Path.cwd()  # 设置工作目录为当前工作目录
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))  # 创建Anthropic客户端实例，使用自定义base_url
MODEL = os.environ["MODEL_ID"]  # 从环境变量获取模型ID
SKILLS_DIR = WORKDIR / "skills"  # 设置技能目录路径


# -- SkillLoader: scan skills/<name>/SKILL.md with YAML frontmatter --  # 注释：SkillLoader：扫描skills/<name>/SKILL.md文件，包含YAML前置元数据
class SkillLoader:  # 定义技能加载器类
    def __init__(self, skills_dir: Path):  # 初始化方法
        self.skills_dir = skills_dir  # 设置技能目录
        self.skills = {}  # 初始化技能字典
        self._load_all()  # 加载所有技能

    def _load_all(self):  # 加载所有技能的方法
        if not self.skills_dir.exists():  # 如果技能目录不存在
            return  # 直接返回
        for f in sorted(self.skills_dir.rglob("SKILL.md")):  # 递归查找所有SKILL.md文件
            text = f.read_text()  # 读取文件内容
            meta, body = self._parse_frontmatter(text)  # 解析前置元数据和主体内容
            name = meta.get("name", f.parent.name)  # 获取技能名称，默认为目录名
            self.skills[name] = {"meta": meta, "body": body, "path": str(f)}  # 存储技能信息

    def _parse_frontmatter(self, text: str) -> tuple:  # 解析前置元数据的方法
        """Parse YAML frontmatter between --- delimiters."""  # 解析---分隔符之间的YAML前置元数据
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)  # 使用正则表达式匹配前置元数据
        if not match:  # 如果没有匹配到前置元数据
            return {}, text  # 返回空元数据和完整文本
        try:
            meta = yaml.safe_load(match.group(1)) or {}  # 解析YAML元数据
        except yaml.YAMLError:  # 如果YAML解析失败
            meta = {}  # 返回空元数据
        return meta, match.group(2).strip()  # 返回元数据和主体内容

    def get_descriptions(self) -> str:  # 获取技能描述的方法（第一层）
        """Layer 1: short descriptions for the system prompt."""  # 第一层：用于系统提示的简短描述
        if not self.skills:  # 如果没有技能
            return "(no skills available)"  # 返回无可用技能
        lines = []  # 初始化行列表
        for name, skill in self.skills.items():  # 遍历所有技能
            desc = skill["meta"].get("description", "No description")  # 获取描述，默认为"No description"
            tags = skill["meta"].get("tags", "")  # 获取标签
            line = f"  - {name}: {desc}"  # 格式化技能行
            if tags:  # 如果有标签
                line += f" [{tags}]"  # 添加标签
            lines.append(line)  # 添加到行列表
        return "\n".join(lines)  # 返回合并后的字符串

    def get_content(self, name: str) -> str:  # 获取技能内容的方法（第二层）
        """Layer 2: full skill body returned in tool_result."""  # 第二层：在工具结果中返回完整技能内容
        skill = self.skills.get(name)  # 获取指定名称的技能
        if not skill:  # 如果技能不存在
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"  # 返回错误信息和可用技能列表
        return f"<skill name=\"{name}\">\n{skill['body']}\n</skill>"  # 返回格式化的技能内容


SKILL_LOADER = SkillLoader(SKILLS_DIR)  # 创建全局技能加载器实例

# Layer 1: skill metadata injected into system prompt  # 第一层：技能元数据注入到系统提示中
SYSTEM = f"""You are a coding agent at {WORKDIR}.  # 系统提示：告诉AI是编码代理
Use load_skill to access specialized knowledge before tackling unfamiliar topics.  # 使用load_skill访问专业知识来处理不熟悉的话题

Skills available:  # 可用技能列表
{SKILL_LOADER.get_descriptions()}"""  # 注入技能描述


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
    "load_skill": lambda **kw: SKILL_LOADER.get_content(kw["name"]),  # 加载技能处理器
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
    {"name": "load_skill", "description": "Load specialized knowledge by name.",  # 加载技能工具定义
     "input_schema": {"type": "object", "properties": {"name": {"type": "string", "description": "Skill name to load"}},
                      "required": ["name"]}},
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
            query = input("\033[36ms05 >> \033[0m")  # 显示青色提示符并获取用户输入
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
