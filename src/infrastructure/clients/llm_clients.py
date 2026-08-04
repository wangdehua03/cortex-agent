from __future__ import annotations

from openai import OpenAI, BadRequestError
from config.config import MODEL, BASE_URL, API_KEY, TOKENIZER_PATH, DEFAULT_TEMPERATURE
from typing import List, Dict, Optional, Callable, Generator
import json
import os
import time
import tiktoken


class _TiktokenFallbackTokenizer:
    """当本地 HuggingFace tokenizer 不可用时，使用 tiktoken cl100k_base 做近似 token 统计。"""

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        self.encoding = tiktoken.get_encoding(encoding_name)

    def encode(self, text: str, add_special_tokens: bool = True) -> list:
        # tiktoken 没有 add_special_tokens 语义，直接返回 token ids
        return self.encoding.encode(text or "")

    def apply_chat_template(self, messages: List[Dict[str, str]], tokenize: bool = True, add_generation_prompt: bool = True):
        # 近似拼接：role + content，最后加上 generation prompt
        parts = []
        for msg in messages:
            parts.append(f"{msg.get('role', 'user')}: {msg.get('content', '')}")
        if add_generation_prompt:
            parts.append("assistant: ")
        text = "\n".join(parts)
        if tokenize:
            return self.encoding.encode(text)
        return text


class LLMClient:

    def __init__(self, base_url: str, api_key: str, max_retries: int = 3, retry_delay: float = 1.0) -> None:
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.tokenizer = self._load_tokenizer()
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        # 是否支持 stream_options={"include_usage": True}（部分 OpenAI 兼容 API 会拒绝该参数）
        self._stream_usage_supported = True

    def _load_tokenizer(self):
        """加载本地 tokenizer，失败或未安装 transformers 时回退到 tiktoken。"""
        if os.path.isdir(TOKENIZER_PATH):
            try:
                from transformers import AutoTokenizer
                return AutoTokenizer.from_pretrained(TOKENIZER_PATH)
            except Exception as e:
                print(f"\033[33m[Tokenizer Warning] 加载本地 tokenizer 失败: {e}，回退到 tiktoken (cl100k_base)\033[0m")
                return _TiktokenFallbackTokenizer()
        print(f"\033[33m[Tokenizer Warning] 本地 tokenizer 路径不存在: {TOKENIZER_PATH}，使用 tiktoken (cl100k_base) 做近似统计\033[0m")
        return _TiktokenFallbackTokenizer()

    def _retry_call(self, func, *args, **kwargs):
        """Retry wrapper for LLM calls. Retries up to max_retries times on any exception."""
        for attempt in range(1, self.max_retries + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if attempt == self.max_retries:
                    raise
                print(f"\033[33m[LLM Retry] Attempt {attempt}/{self.max_retries} failed: {e}. Retrying in {self.retry_delay}s...\033[0m")
                time.sleep(self.retry_delay)

    def _count_tokens(self, messages: List[Dict[str, str]]) -> int:
        """
        使用 tokenizer.apply_chat_template 精确计算消息列表的 token 数量。
        兼容 tool_calls/tool_call_id 等额外字段：只提取 role/content 用于 chat template，
        tool_calls 序列化后追加到 assistant 的 content 中。
        """
        conv_messages = []
        for msg in messages:
            role = msg.get("role", "")
            content = self._clean_message_content(msg.get("content", ""))
            # 如果有 tool_calls，将序列化的 tool_calls 追加到 content，让 tokenizer 能统计到
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                try:
                    content = content + "\n" + json.dumps(tool_calls, ensure_ascii=False)
                except (TypeError, ValueError):
                    pass
            conv_messages.append({"role": role, "content": content})

        try:
            encoded = self.tokenizer.apply_chat_template(conv_messages, tokenize=True, add_generation_prompt=True)
            return len(encoded)
        except Exception:
            # fallback: 如果 tokenizer 不支持 chat_template，回退到逐条 encode 累加
            total = 0
            for msg in conv_messages:
                total += self.tokenizer.encode(msg["content"], add_special_tokens=False).__len__()
            return total

    def _count_string_tokens(self, text: str) -> int:
        """
        计算字符串的 token 数量
        """
        return len(self.tokenizer.encode(text))
    
    def _clean_message_content(self, content) -> str:
        """
        清理消息内容，确保是有效的字符串
        """
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        # 如果是其他类型，转换为字符串
        return str(content)
    
    def _repair_json_arguments(self, raw_args: str) -> str:
        """
        尝试修复不完整的 JSON arguments 字符串，确保返回合法的 JSON 字符串。
        按优先级尝试：直接解析 -> 补全括号 -> 正则提取键值对
        """
        if not raw_args or not raw_args.strip():
            return '{}'

        stripped = raw_args.strip()
        if stripped in ('{', '{\n', '{ ', '{\t'):
            return '{}'

        # 1) 直接解析
        try:
            json.loads(raw_args)
            return raw_args
        except json.JSONDecodeError:
            pass

        # 2) 补全缺失的闭合括号
        test_str = raw_args
        test_str += '}' * max(0, test_str.count('{') - test_str.count('}'))
        test_str += ']' * max(0, test_str.count('[') - test_str.count(']'))
        try:
            json.loads(test_str)
            return test_str
        except json.JSONDecodeError:
            pass

        # 3) 正则提取 "key": "value" 键值对
        import re as _re
        matches = _re.findall(r'"([^"]+)"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_args, _re.DOTALL)
        if matches:
            args_dict = {}
            for k, v in matches:
                try:
                    args_dict[k] = json.loads(f'"{v}"')
                except (json.JSONDecodeError, ValueError):
                    args_dict[k] = v
            return json.dumps(args_dict, ensure_ascii=False)

        return '{}'
    
    def _clean_messages(self, msg_list: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """
        清理消息列表，确保所有字段都是有效的字符串
        """
        cleaned = []
        for msg in msg_list:
            cleaned_msg = {}
            for key, value in msg.items():
                if key == 'tool_calls':
                    # 保留 tool_calls 结构
                    cleaned_msg[key] = value
                elif key == 'tool_call_id':
                    cleaned_msg[key] = str(value) if value is not None else ""
                elif key == 'content':
                    cleaned_msg[key] = self._clean_message_content(value)
                else:
                    cleaned_msg[key] = str(value) if value is not None else value
            cleaned.append(cleaned_msg)
        return cleaned

    def chat(self,
             msg_list: List[Dict[str,str]],
             stream: bool = False,
             model = MODEL,
             tools: List[Dict[str,str]] | None = None,
             max_tokens: int = 2048,
             temperature = DEFAULT_TEMPERATURE,
             timeout=180):
        """Args:
        msg_list: [{"role":"user","content":"xxxx"}]
        stream: 是否使用流式输出"""
        return self._retry_call(
            self.client.chat.completions.create,
            model=model,
            messages=msg_list,
            max_tokens=max_tokens,
            tools=tools,
            temperature=temperature,
            stream=stream,
            timeout=timeout
        )

    def chat_stream(self,
                    msg_list: List[Dict[str, str]],
                    model: str = MODEL,
                    tools: List[Dict[str, str]] | None = None,
                    max_tokens: int = 2048,
                    temperature: float = DEFAULT_TEMPERATURE,
                    timeout: int = 180,
                    on_chunk: Optional[Callable[[str], None]] = None) -> 'StreamResponse':
        """
        流式调用 LLM，支持工具调用（带自动重试）

        Args:
            msg_list: 消息列表
            model: 模型名称
            tools: 工具定义列表
            max_tokens: 最大 token 数
            temperature: 温度参数
            timeout: 超时时间
            on_chunk: 每个流式块的回调函数，接收字符串参数

        Returns:
            StreamResponse 对象，结构与 OpenAI 响应类似
        """
        return self._retry_call(
            self._chat_stream_impl,
            msg_list=msg_list,
            model=model,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            on_chunk=on_chunk
        )

    def _chat_stream_impl(
            self,
            msg_list: List[Dict[str, str]],
            model: str,
            tools: List[Dict[str, str]] | None,
            max_tokens: int,
            temperature: float,
            timeout: int,
            on_chunk: Optional[Callable[[str], None]]) -> 'StreamResponse':
        """chat_stream 的实际实现，由 _retry_call 包裹重试。"""
        # 请求参数：优先让 API 在流式响应末尾返回 usage（真实计费 token 数）
        create_kwargs = dict(
            model=model,
            messages=msg_list,
            max_tokens=max_tokens,
            tools=tools,
            temperature=temperature,
            stream=True,
            timeout=timeout
        )
        if self._stream_usage_supported:
            create_kwargs["stream_options"] = {"include_usage": True}

        try:
            stream = self.client.chat.completions.create(**create_kwargs)
        except BadRequestError as e:
            # 兼容不支持 stream_options 的 API：关闭后重试一次，并记住不再传该参数
            if self._stream_usage_supported and 'stream_options' in str(e):
                print("\033[33m[LLM Warning] API 不支持 stream_options，回退为本地 token 估算\033[0m")
                self._stream_usage_supported = False
                create_kwargs.pop("stream_options", None)
                stream = self.client.chat.completions.create(**create_kwargs)
            else:
                raise

        # 累积流式响应
        full_content = ""
        tool_calls_data = {}  # 映射 index -> tool_call data
        usage_info = None
        finish_reason = None  # 捕获 finish_reason

        for chunk in stream:
            # 记录 usage 信息（通常在最后一个 chunk）
            if chunk.usage:
                usage_info = chunk.usage

            if chunk.choices and len(chunk.choices) > 0:
                choice = chunk.choices[0]

                # 捕获 finish_reason（通常在最后一个 chunk）
                if choice.finish_reason:
                    finish_reason = choice.finish_reason

                delta = choice.delta

                # 累积文本内容
                if delta.content:
                    full_content += delta.content
                    if on_chunk:
                        on_chunk(delta.content)

                # 累积工具调用
                if delta.tool_calls:
                    for tool_call_delta in delta.tool_calls:
                        idx = tool_call_delta.index

                        # 初始化
                        if idx not in tool_calls_data:
                            tool_calls_data[idx] = {
                                'id': '',
                                'tool_type': 'function',
                                'function': {
                                    'name': '',
                                    'arguments': ''
                                }
                            }

                        # 累积 ID
                        if tool_call_delta.id:
                            tool_calls_data[idx]['id'] += tool_call_delta.id

                        # 累积 function 信息
                        if tool_call_delta.function:
                            if tool_call_delta.function.name:
                                tool_calls_data[idx]['function']['name'] = tool_call_delta.function.name
                            if tool_call_delta.function.arguments:
                                tool_calls_data[idx]['function']['arguments'] += tool_call_delta.function.arguments

        # 修复不完整的 JSON arguments（LLM 有时会生成不完整的 JSON）
        for idx in tool_calls_data:
            args_str = tool_calls_data[idx]['function']['arguments']
            repaired_args = self._repair_json_arguments(args_str)
            tool_calls_data[idx]['function']['arguments'] = repaired_args

        # 构建完整的 tool_calls 列表
        full_tool_calls = list(tool_calls_data.values()) if tool_calls_data else None

        # 如果 usage_info 存在且有效，使用它；否则一次性对完整输出计算 token 数
        if usage_info and (getattr(usage_info, 'prompt_tokens', 0) > 0 or getattr(usage_info, 'completion_tokens', 0) > 0):
            # 使用 API 返回的 usage 信息（真实计费 token 数，最准确）
            pass
        else:
            # fallback：API 未返回 usage 时用本地 tokenizer 近似估算
            # 仅在 fallback 时 encode 完整文本一次，避免逐 chunk 编码的碎片效应
            prompt_tokens = self._count_tokens(msg_list)
            completion_text = full_content
            for tc in tool_calls_data.values():
                completion_text += tc['function']['arguments']
            completion_tokens = self._count_string_tokens(completion_text)
            total_tokens = prompt_tokens + completion_tokens
            usage_info = type('Usage', (object,), {
                'prompt_tokens': prompt_tokens,
                'completion_tokens': completion_tokens,
                'total_tokens': total_tokens
            })()

        # 返回 StreamResponse 对象
        return StreamResponse(
            content=full_content,
            tool_calls=full_tool_calls,
            usage=usage_info,
            finish_reason=finish_reason
        )


class StreamToolCall:
    """流式工具调用对象"""
    def __init__(self, id: str, tool_type: str, function: dict):
        self.id = id
        self.type = tool_type
        self.function = type('Function', (object,), function)()
    
    def model_dump(self, exclude_none=True):
        result = {
            'id': self.id,
            'type': self.type,
            'function': {
                'name': self.function.name,
                'arguments': self.function.arguments
            }
        }
        
        if exclude_none:
            # 递归过滤 None 值
            result = {k: v for k, v in result.items() if v is not None}
            if 'function' in result:
                result['function'] = {k: v for k, v in result['function'].items() if v is not None}
        
        return result


class StreamMessage:
    """流式消息对象"""
    def __init__(self, content: str, tool_calls: list):
        self.content = content
        self.role = "assistant"
        self.tool_calls = [StreamToolCall(**tc) for tc in tool_calls] if tool_calls else None
    
    def model_dump(self, exclude_none=True):
        result = {
            'role': self.role,
        }
        if self.content is not None:
            result['content'] = self.content
        if self.tool_calls is not None:
            result['tool_calls'] = [tc.model_dump(exclude_none) for tc in self.tool_calls]
        
        if exclude_none:
            result = {k: v for k, v in result.items() if v is not None}
        
        return result


class StreamResponse:
    """流式响应对象，模拟 OpenAI 响应结构"""
    def __init__(self, content: str, tool_calls: list, usage=None, finish_reason=None):
        # 按照 OpenAI 结构，finish_reason 在 choice 层级
        self.choices = [type('Choice', (object,), {
            'message': StreamMessage(content=content, tool_calls=tool_calls),
            'finish_reason': finish_reason
        })()]
        # 使用真实的 usage 信息，如果为 None 则创建默认值
        if usage is not None:
            self.usage = usage
        else:
            self.usage = type('Usage', (object,), {
                'prompt_tokens': 0,
                'completion_tokens': 0,
                'total_tokens': 0
            })()


if __name__ == '__main__':
    llm = LLMClient(base_url=BASE_URL, api_key=API_KEY)
    msg_list = [{"role":"user","content":"请介绍一下你自己"}]
    
    # 测试流式输出
    print("流式输出测试：")
    response = llm.chat_stream(
        msg_list=msg_list, 
        on_chunk=lambda text: print(text, end='', flush=True)
    )
    print()
    print(f"\n内容: {response.choices[0].message.content}")
    print(f"工具调用: {response.choices[0].message.tool_calls}")
