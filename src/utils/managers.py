from typing import Dict
from pathlib import Path
import re, yaml

class TodoManager:

    def __init__(self) -> None:
        self.items = []

    def update(self, items: list, **kwargs) -> str:
        # 忽略额外参数（如 summary 等），只使用 items 参数
        if len(items) > 20:
            return f"[Todo Error] Max 20 todos allowed, but got {len(items)}. Please reduce the number of todos."
        
        validated = []
        in_progress_count = 0
        errors = []
        
        for i, item in enumerate(items):
            # 兼容 text 和 content 字段，优先使用 text
            text = str(item.get("text") or item.get("content", "")).strip()
            status = str(item.get("status","pending")).lower()
            item_id = str(item.get("id", str(i+1)))
            
            if not text:
                errors.append(f"Item {item_id}: text is required but was empty.")
                continue
            if status not in ("pending","in_progress","completed"):
                errors.append(f"Item {item_id}: invalid status '{status}'. Must be one of: pending, in_progress, completed.")
                continue
            if status == "in_progress":
                in_progress_count += 1
            validated.append({"id":item_id, "text":text, "status":status})
        
        # 如果有验证错误，返回错误信息
        if errors:
            error_msg = "[Todo Error] The following issues were found:\n"
            for err in errors:
                error_msg += f"  - {err}\n"
            error_msg += "\nPlease fix these issues and try again. Only include valid todos in your update."
            return error_msg
        
        # 检查 in_progress 数量
        if in_progress_count > 1:
            return f"[Todo Error] Only one task can be 'in_progress' at a time, but found {in_progress_count}. Please set only one task as 'in_progress' and others as 'pending' or 'completed'."
        
        self.items = validated
        return self.render()
    
    def render(self) -> str:
        if not self.items:
            return "No todos."
        lines = []
        for item in self.items:
            marker = {"pending":"[]", "in_progress":"[>]","completed":"[x]"}[item["status"]]
            lines.append(f"{marker} #{item['id']}:{item['text']}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)
    
    def clear(self) -> None:
        # 清除所有待办项
        self.items = []




class SkillLoader:
    """
    技能加载器：支持两种模式
    1. Registry Mode: 只加载技能描述到agent system prompt (轻量)
    2. Full Mode: 完整加载指定技能的所有内容
    """

    def __init__(self, skills_dir: str) -> None:
        self.skills_dir = Path(skills_dir)
        self._registry: Dict[str,str] = {} # 技能注册表 skill_name:skill_path
        self.discover()

    def discover(self):
        """扫描 skills_dir 目录, 发现所有可用技能"""
        for item in self.skills_dir.iterdir():
            if item.is_dir() and (item / "SKILL.md").exists():
                self._registry[item.name] = item.as_posix()

    def get_descriptions(self, skill_path: Path) -> str:
        skill_md = Path(skill_path / "SKILL.md").read_text()
        meta, _ = self._parse_frontmatter(skill_md)
        if meta:
            return f"name: {meta.get('name')}\ndescription: {meta.get('description')}"
        else:
            return ""


    def _parse_frontmatter(self, text: str) -> dict:
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        return meta, text
        

    def build_registry_prompt(self) -> str:
        """
        构建技能注册表以便在sys_prompt中使用
        """
        prompt = ""
        for name, skill_path in sorted(self._registry.items()):
            prompt += f"{self.get_descriptions(Path(skill_path))}\n\n"
        return prompt
    
    def load_full_skill(self, skill_name: str) -> dict:
        """
        完整加载指定技能的所有内容
        包括：SKILL.md + README.md + examples + scripts 路径
        """
        if skill_name not in self._registry:
            return None
        
        skill = {}
        skill_path = Path(self._registry[skill_name])
        # 读取 SKILL.md 完整内容
        skill_md = Path(skill_path / "SKILL.md").read_text()
        skill["skill_md"] = skill_md
        # 读取 README.md
        readme_path = Path(skill_path / "README.md")
        if readme_path.exists():
            readme_md = readme_path.read_text()
            skill["readme_md"] = readme_md
        # 读取 examples
        examples_dir = Path(skill_path / "examples")
        if examples_dir.exists():
            skill_examples = [
                f.read_text()
                for f in sorted(examples_dir.glob("*.md"))
            ]
            skill["skill_examples"] = skill_examples
        # 扫描 scripts
        scripts_dir = Path(skill_path / "scripts")
        if scripts_dir.exists():
            skill_scripts = {
                f.name: f
                for f in scripts_dir.iterdir()
                if f.is_file()
            }
            skill["skill_scripts"] = skill_scripts
        return skill
    
    def build_full_skill_prompt(self, skill_name: str):
        """构建完整的技能prompt片段"""
        skill = self.load_full_skill(skill_name)
        if not skill:
            return None
        
        sections = [
            f"# Active Skill: {skill_name}",
            "\n",
            "## Skill Instructions",
            skill["skill_md"],
        ]

        # # 追加 README（如果存在）
        # if "readme_md" in skill:
        #     sections.extend([
        #         "\n",
        #         "## Overview / README",
        #         skill["readme_md"]
        #     ])

        # 追加 examples(可选，控制长度)
        if "skill_examples" in skill:
            sections.extend([
                "\n",
                "## Examples",
                "\n\n---\n\n".join(skill["skill_examples"][:2]) # 最多2个示例，防膨胀
            ])

        # 追加 scripts 可用性说明
        if "skill_scripts" in skill:
            sections.extend([
                "\n",
                "## Available Scripts",
                "The following scripts are available for tool use:",
                "\n".join(f"- `{name}`: {path}" for name, path in skill["skill_scripts"].items())
            ])
    

        return "\n".join(sections)




if __name__ == "__main__":
    s = SkillLoader("/home/dev2/PyProject/wangdehua/projects/agent_platform/skills")
    print('test')

