import os
import re
import datetime
from typing import Literal
from deepagents import create_deep_agent, FilesystemPermission
from deepagents.backends import CompositeBackend,FilesystemBackend
from langchain.tools import tool
from ddgs import DDGS

import requests
from bs4 import BeautifulSoup
from models import model


DEFAULT_DIR = './memories'
WORKDIR = "./knowledge-trees"
SKILLS_DIR = "./skills"

SYSTEM_PROMPT = """
  你是一个知识问答助手。你将根据用户的问题，搜索网络，获取网页内容，或者分析本地文件，给出回答。
  你可以使用以下工具：
  1. `search_web(query: str, max_results: int = 5)`：在网络上搜索相关信息，返回搜索结果列表。
  2. `fetch_webpage(url: str, max_length: int = 15000)`：获取指定网页的内容，返回网页文本。
"""

@tool
def fetch_webpage(url: str, max_length: int = 15000) -> str:
    """Fetch the content of a webpage given its URL."""
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        # 自动检测编码：处理 requests 默认的 ISO-8859-1 和未指定编码的情况
        if response.encoding is None or response.encoding.lower() in ('iso-8859-1', 'iso8859-1'):
            response.encoding = response.apparent_encoding or 'utf-8'
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 删除script和style标签
        for script in soup(["script", "style", "nav", "footer"]):
            script.decompose()
        
        # 获取主要文本内容
        text = soup.get_text(separator='\n', strip=True)
        return text[:max_length] if text else "未找到文本内容"
        
    except Exception as e:
        return f"获取页面失败: {str(e)}"

@tool
def search_web(query: str, max_results: int = 5) -> list:
    """Search the web using DuckDuckGo and return a list of results."""
    ddgs = DDGS()
    results = []
    try:
        for r in ddgs.text(query, max_results=max_results):
            results.append(r)
    except Exception as e:
        results.append(f"搜索失败: {str(e)}")
    return results

@tool
def get_current_date() -> str:
    """Return the current date in YYYY/MM/DD format."""
    return datetime.datetime.now().strftime("%Y/%m/%d")

def ensure_parent_dir(full_path: str) -> None:
    """确保目标文件所在目录存在。

    知识树目录（WORKDIR）由 agent 在运行时生成，不纳入版本控制，
    因此全新克隆的仓库中并不存在。首次写入前自动创建，避免 FileNotFoundError。
    """
    os.makedirs(os.path.dirname(full_path), exist_ok=True)

@tool
def append_leaf(path: str, tree_name: str, content: str) -> str:
    """Append content to a knowledge tree file at the given path. If the file does not exist, it will be created with a basic structure.
    Returns a message indicating success or failure.
    path: The relative path to the knowledge tree file (e.g., 'python.md').
    tree_name: The name of the knowledge tree.
    content: The content to append, which should be formatted as a third-level heading with date, title, and body.
    """
    full_path = os.path.abspath(os.path.join(WORKDIR, path))
    
    if not full_path.startswith(os.path.abspath(WORKDIR) + os.sep):
        return f"不允许使用绝对路径或目录遍历"

    # 目录可能尚不存在（如全新克隆的仓库），首次写入时自动创建
    ensure_parent_dir(full_path)

    # 文件不存在则创建（含基础结构）
    if not os.path.exists(full_path):
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(f"# {tree_name}\n\n\n## 🍂 待整理\n")  # 初始化文件内容

    # 追加到文件末尾"待整理"区域；
    try:
        with open(full_path, 'a', encoding='utf-8') as f:
            f.write(content + "\n")
        return f"已将内容追加到 {path}"
    except Exception as e:
        return f"写入文件失败: {str(e)}"


# 以下为知识树整理相关功能
def insert_node(md_text: str, target_heading: str, insert_content: str, insert_at: Literal["start", "end"]) -> str:
    """把 insert_content 插到target_heading 这个节点内容的最开头或最末尾"""
    lines = md_text.split('\n')
    # 1. 找到目标标题所在行
    idx = None
    for i, line in enumerate(lines):
        if line.strip() == target_heading:
            idx = i 
            break
    if idx is None:
        raise ValueError(f"未找到节点: {target_heading}")

    # 2. 确定插入位置
    if insert_at == "start":    
        insert_pos = idx + 1  # 在标题下一行插入
    elif insert_at == "end":
        # 从标题下一行起，找下一个同级别标题作为本节点边界。当下一节点不存在时，匹配更高层级标题作为本次插入的边界
        target_heading_level = len(target_heading) - len(target_heading.lstrip('#'))  # 计算标题层级
        insert_pos = len(lines)  # 默认到文件末尾
        for j in range(idx + 1, len(lines)):
            if re.match(r'^#{1,' + str(target_heading_level) + r'}\s', lines[j].strip()):  # 下一个节点/更高层标题
                insert_pos = j
                break
    else:
        raise ValueError(f"无效的插入位置: {insert_at}")

    # 3. 在边界前插入（补一个空行做间距，保证 md 渲染正确）
    new_lines = lines[:insert_pos] + ['', insert_content.rstrip('\n')] + lines[insert_pos:]
    return '\n'.join(new_lines)

def move_node(md_text: str, source_heading: str, target_heading: str, insert_at: Literal["start", "end"]) -> str:
    """把 source_heading 这个节点内容移动到 target_heading 这个节点内容的最开头或最末尾"""
    lines = md_text.split('\n')
    # 1. 找到源标题和目标标题所在行
    source_idx = None
    target_idx = None
    for i, line in enumerate(lines):
        if line.strip() == source_heading:
            source_idx = i
        if line.strip() == target_heading:
            target_idx = i
        if source_idx is not None and target_idx is not None:
            break
    if source_idx is None:
        raise ValueError(f"未找到源节点: {source_heading}")
    if target_idx is None:
        raise ValueError(f"未找到目标节点: {target_heading}")

    # 2. 提取源节点内容（包括标题和正文）
    source_level = len(source_heading) - len(source_heading.lstrip('#'))
    end_idx = len(lines)
    for j in range(source_idx + 1, len(lines)):
        if re.match(r'^#{1,' + str(source_level) + r'}\s', lines[j].strip()):
            end_idx = j
            break
    source_content = '\n'.join(lines[source_idx:end_idx])

    # 3. 删除源节点内容
    del lines[source_idx:end_idx]

    # 4. 插入到目标节点位置
    new_md_text = '\n'.join(lines)
    new_md_text = insert_node(new_md_text, target_heading, source_content, insert_at)

    return new_md_text

def get_pending_leaf_count(md_text: str) -> int:
    "获取待整理区的树叶的数量"
    lines = md_text.split('\n')
    # 1. 找到待整理区域
    pending_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "## 🍂 待整理":
            pending_idx = i
            break
    if pending_idx is None:
        raise ValueError("未找到待整理区域")

    leaf_count = 0
    for j in range(pending_idx + 1, len(lines)):
        if re.match(r"^### \[\d", lines[j].strip()):  # 匹配三级标题开头的叶子节点
            leaf_count += 1
    return leaf_count
  

@tool
def cleanup_organized_pending_leaf(path: str, leaf_heading:str) -> str:
    """把已整理的叶子节点从待整理区域移除
    path: 知识树文件的相对路径（例如 'python.md'）。
    leaf_heading: 已整理的叶子节点标题（例如 '### [2023/08/01] 学习 Python'）
    """
    full_path = os.path.abspath(os.path.join(WORKDIR, path))
    
    if not full_path.startswith(os.path.abspath(WORKDIR) + os.sep):
        return f"不允许使用绝对路径或目录遍历"

    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            md_text = f.read()
    except FileNotFoundError:
        return f"文件未找到: {path}"

    lines = md_text.split('\n')
    # 1. 找到待整理区域
    pending_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "## 🍂 待整理":
            pending_idx = i
            break
    if pending_idx is None:
        return f"未找到待整理区域: {path}"

    # 2. 找到叶子节点所在行
    leaf_idx = None
    for j in range(pending_idx + 1, len(lines)):
        if lines[j].strip() == leaf_heading:
            leaf_idx = j
            break
    if leaf_idx is None:
        return f"未找到叶子节点: {leaf_heading}"

    # 3. 删除叶子节点内容（包括标题和正文）
    leaf_level = len(leaf_heading) - len(leaf_heading.lstrip('#'))
    end_idx = len(lines)
    for k in range(leaf_idx + 1, len(lines)):
        if re.match(r'^#{1,' + str(leaf_level) + r'}\s', lines[k].strip()):
            end_idx = k
            break
    del lines[leaf_idx:end_idx]

    new_md_text = '\n'.join(lines)
    try:
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(new_md_text)
    except Exception as e:
        return f"写入文件失败: {str(e)}"    

    leaf_count = get_pending_leaf_count(new_md_text)
    return f"知识树已更新: {path}, 待整理区剩余 {leaf_count} 个叶子节点。"

@tool
def organize_tree(
 path: str,
 op: Literal["insert_node","move_node"],
 anchor: str, # 定位锚点节点标题
 insert_at: Literal["start","end"], # 追加的位置
 content: str ,# 新建的叶子或子节点,
 source_heading: str = None
) -> str:
    """
    向知识树文件中插入新节点、移动节点或清理已整理的叶子节点。
    path: 知识树文件的相对路径（例如 'python.md'）。
    op: 操作类型，可选值为 "insert_node"（插入新节点）、"move_node"（移动节点）。
    anchor: 定位锚点节点标题，用于确定插入或移动的位置。
    insert_at: 追加的位置，可选值为 "start"（在锚点节点内容的开头插入）或 "end"（在锚点节点内容的末尾插入）。
    content: 新建的叶子或子节点文本
    例如：
    ### 新增节点标题
    节点正文内容。
    """
    full_path = os.path.abspath(os.path.join(WORKDIR, path))
    if not full_path.startswith(os.path.abspath(WORKDIR) + os.sep):
            return f"不允许使用绝对路径或目录遍历"

    # 目录可能尚不存在（如全新克隆的仓库）
    ensure_parent_dir(full_path)

    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            md_text = f.read()
    except FileNotFoundError:
        return f"文件未找到: {path}"

    if op == "insert_node":
        new_md_text = insert_node(md_text, anchor, content, insert_at)
    elif op == "move_node":
        new_md_text = move_node(md_text, source_heading, anchor, insert_at)
    else:
        raise ValueError(f"不支持的操作: {op}")

    try:
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(new_md_text)
    except Exception as e:
        return f"写入文件失败: {str(e)}"

    return f"知识树已更新: {path}"

@tool
def update_index(content: str) -> str:
    """更新知识树索引文件, 如果不存在, 将创建一个新的索引文件。
    content: 整份索引文件的内容（覆盖式写入）
    索引样例：
    - [Python](./programming.md) — Python、Deep Agents、软件工程
    - [心理学](./psychology.md) — 执行功能、认知偏误、禅宗
    """
    index_path = os.path.join(WORKDIR, "index.md")

    # 目录可能尚不存在（如全新克隆的仓库），首次写入时自动创建
    ensure_parent_dir(index_path)

    if not os.path.exists(index_path):
            content = "# 知识树索引\n\n" + content  # 如果索引文件不存在，添加标题
    try:
        with open(index_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return "索引文件已更新"
    except Exception as e:
        return f"写入索引文件失败: {str(e)}"


# `langgraph.json` points at this module-level variable: "./agent.py:graph".
graph = create_deep_agent(
  model=model, 
  backend= CompositeBackend(
    default=FilesystemBackend(root_dir=DEFAULT_DIR, virtual_mode=True),
    routes={
        '/knowledge-trees/': FilesystemBackend(root_dir=WORKDIR, virtual_mode=True),
        '/skills/': FilesystemBackend(root_dir=SKILLS_DIR, virtual_mode=True),
        }),
    permissions=[
        FilesystemPermission(operations=["write"], paths=['/skills/'],mode="deny",),
        FilesystemPermission(operations=["write"], paths=['/knowledge-trees/'],mode="deny",),
    ],
  system_prompt=SYSTEM_PROMPT,
  skills=['/skills/'],
  tools=[search_web, fetch_webpage, append_leaf, get_current_date, organize_tree, cleanup_organized_pending_leaf, update_index],
  interrupt_on={
      "append_leaf": {"allowed_decisions": ["approve", "edit", "reject"]},
      "organize_tree": {"allowed_decisions": ["approve", "edit", "reject"]},
      "cleanup_organized_pending_leaf": {"allowed_decisions": ["approve", "reject"]},
      "update_index": {"allowed_decisions": ["approve", "edit", "reject"]},
  }
)