# --- START OF FILE api_service.py ---
#
# ==============================================================================
#  API 集成更新摘要 (API Integration Update Summary)
# ==============================================================================
#
#  版本: v2.2.1
#  更新日期: 2025年09月14日
#  更新人员: AI Assistant
#
#  重大变更:
#  1. API Key格式处理增强 - 解决用户输入格式不一致的问题
#     * 新增 `_preprocess_api_key` 方法，统一处理不同格式的API Key
#     * 腾讯API Key: 支持中文冒号自动转换，增强格式验证
#     * Bearer Token: 智能移除重复的"Bearer "前缀
#     * 提供详细的错误提示和格式指导
#  2. 统一兼容Payload构建器 - 修正并统一了所有OpenAI兼容模型的请求构建逻辑。
#     * `_build_openai_compatible_payload` 现遵循"图片在前，文本在后"的最大兼容原则。
#     * 阿里云、百度、Moonshot、智谱等统一使用此构建器，大幅减少代码冗余。
#     * 删除了重复的 `_build_aliyun_payload` 和 `_build_baidu_payload` 函数。
#  3. 百度文心千帆V2 API升级 - 从旧版API迁移到全新V2版本
#     * Endpoint: https://qianfan.baidubce.com/v2/chat/completions
#     * 鉴权方式: Bearer token (bce-v3/ALTAK-...格式)
#     * 请求格式: 与OpenAI接口高度兼容
#     * 响应解析: 标准 choices[0].message.content 格式
#  4. 腾讯混元 API 集成更新 - 统一使用 ChatCompletions 接口
#     * 从 ImageQuestion 迁移到 ChatCompletions action (无频率限制)
#     * 实现腾讯云 TC3-HMAC-SHA256 签名方法 v3
#     * 智能模型适配 - 支持所有腾讯视觉模型的自动检测和适配
#     * 最大兼容性 - 用户输入的任何腾讯视觉模型都能正确调用
#
#  支持的视觉模型:
#  百度文心千帆:
#  - # deepseek-vl2 (推荐) - 2025/9/14，deepseek官方未提供视觉模型，暂时不使用
#  - ernie-4.5-vl-28b-a3b (深度思考)
#  - qwen2.5-vl 系列
#  - llama-4-maverick-17b-128e-instruct (多图输入)
#  - internvl2_5-38b-mpo
#
#  腾讯混元:
#  - hunyuan-vision (基础多模态模型)
#  - hunyuan-turbos-vision (旗舰视觉模型)
#  - hunyuan-turbos-vision-20250619 (最新旗舰版本)
#  - hunyuan-t1-vision (深度思考视觉模型)
#  - hunyuan-t1-vision-20250619 (最新深度思考版本)
#  - hunyuan-large-vision (多语言视觉模型)
#
#  技术特性:
#  - API Key 格式: Bearer bce-v3/ALTAK-... (百度) / SecretId:SecretKey (腾讯)
#  - 鉴权方式: Bearer token / 腾讯云签名方法 v3
#  - 接口类型: ChatCompletions (兼容OpenAI格式)
#  - 图像格式: JPEG base64编码
#  - 响应解析: 标准 choices[0].message.content 格式
#
#  未来维护指南:
#  1. 新模型适配: 监控各厂商官方文档更新
#  2. API变更: 及时跟进接口格式变化
#  3. 错误处理: 关注签名过期和服务错误码
#  4. 性能优化: 注意请求频率和超时设置
#  5. 兼容性: 保持与OpenAI接口的兼容性
#
# ==============================================================================

import requests
import logging
import traceback
from typing import Tuple, Optional, Dict, Any
import hashlib
import hmac
import time
import json
from datetime import datetime

# ==============================================================================
#  UI文本到提供商ID的映射字典 (UI Text to Provider ID Mapping)
#  这是连接UI显示文本和后台代码的桥梁。
#  UI上的"火山引擎 (豆包)" 对应到代码里的 "volcengine"。
#  现在基于 PROVIDER_CONFIGS 动态生成，避免数据冗余。
#  注意：只包含AI评分模型提供商，不包含OCR服务提供商
# ==============================================================================
def generate_ui_text_to_provider_id():
    """基于 PROVIDER_CONFIGS 动态生成 UI_TEXT_TO_PROVIDER_ID 映射
    
    排除OCR服务提供商（如baidu_ocr），只包含AI评分模型提供商
    OCR功能是一个独立的工作模式，不应该出现在AI模型选择下拉框中
    """
    # OCR服务不是AI评分模型，应该排除
    OCR_PROVIDERS = {'baidu_ocr'}
    return {
        config["name"]: provider_id 
        for provider_id, config in PROVIDER_CONFIGS.items()
        if provider_id not in OCR_PROVIDERS
    }

# ==============================================================================
#  权威供应商配置字典 (Authoritative Provider Configuration)
#  这是整个系统的"单一事实来源 (Single Source of Truth)"。
#
#  腾讯混元更新历史 (Tencent Hunyuan Update History):
#  - 2025-09-13: 重大更新 - 统一使用 ChatCompletions 接口
#    * 替换 ImageQuestion 为 ChatCompletions action (无频率限制)
#    * 实现腾讯云签名方法 v3 完整认证
#    * 支持所有视觉模型自动适配 (hunyuan-vision, hunyuan-turbos-vision 等)
#    * 智能检测视觉模型并自动选择正确的 payload 格式
#    * API Key 格式: SecretId:SecretKey
# ==============================================================================
PROVIDER_CONFIGS = {
    # 这里的 key ('volcengine', 'moonshot'等) 是程序内部使用的【内部标识】
    "volcengine": {
        "name": "火山引擎 (推荐)",
        "url": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        "auth_method": "bearer",
        "payload_builder": "_build_volcengine_payload",
    },
    "moonshot": {
        "name": "月之暗面",
        "url": "https://api.moonshot.cn/v1/chat/completions",
        "auth_method": "bearer",
        "payload_builder": "_build_openai_compatible_payload",
    },
    "zhipu": {
        "name": "智谱清言",
        "url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "auth_method": "bearer", # 智谱的Key虽然是JWT，但用法和Bearer完全一样
        "payload_builder": "_build_openai_compatible_payload",
    },
    # "deepseek": {
    #     "name": "deepseek",
    #     "url": "https://api.deepseek.com/chat/completions",
    #     "auth_method": "bearer",
    #     "payload_builder": "_build_openai_compatible_payload",
    # },
    "aliyun": {
        "name": "阿里通义千问",
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "auth_method": "bearer",
        "payload_builder": "_build_openai_compatible_payload",
    },
    "baidu": {
        "name": "百度文心千帆",
        "url": "https://qianfan.baidubce.com/v2/chat/completions",
        "auth_method": "bearer",
        "payload_builder": "_build_openai_compatible_payload",
    },
    "tencent": {
        "name": "腾讯混元",
        "url": "https://hunyuan.tencentcloudapi.com/",
        "auth_method": "tencent_signature_v3", # 使用腾讯云签名方法 v3
        "payload_builder": "_build_tencent_payload",
        "service_info": {  # 新增服务信息配置，避免硬编码
            "service": "hunyuan",
            "region": "ap-guangzhou",
            "version": "2023-09-01",
            "host": "hunyuan.tencentcloudapi.com",
            "action": "ChatCompletions"
        }
    },
    "openrouter": {
        "name": "OpenRouter",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "auth_method": "bearer",
        "payload_builder": "_build_openai_compatible_payload",
    },
    "openai": { # 新增
        "name": "OpenAI",
        "url": "https://api.openai.com/v1/chat/completions",
        "auth_method": "bearer",
        "payload_builder": "_build_openai_compatible_payload",
    },
    "gemini": { # 新增
        "name": "Google Gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",  # {model} 将被动态替换
        "auth_method": "google_api_key_in_url",
        "payload_builder": "_build_gemini_payload",
        "dynamic_url": True,  # 标记需要动态URL替换
    }
}

# ==============================================================================
#  生成UI文本到提供商ID的映射常量
# ==============================================================================
UI_TEXT_TO_PROVIDER_ID = generate_ui_text_to_provider_id()

# ==============================================================================
#  辅助函数，用于UI和内部ID之间的转换
# ==============================================================================
def get_provider_id_from_ui_text(ui_text: str) -> Optional[str]:
    mapping = generate_ui_text_to_provider_id()
    return mapping.get(ui_text.strip())

def get_ui_text_from_provider_id(provider_id: str) -> Optional[str]:
    config = PROVIDER_CONFIGS.get(provider_id)
    return config["name"] if config else None

class ApiService:
    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.session = requests.Session()
        self.logger = logging.getLogger(__name__)
        # 初始化当前题目索引，虽然主要逻辑在AutoThread中，但这里有个默认值更安全
        self.current_question_index = 1

    # ==========================================================================
    #  腾讯云签名方法 v3 实现 (Tencent Cloud Signature Method v3)
    #
    #  更新历史 (Update History):
    #  - 2025-09-13: 首次实现完整的 TC3-HMAC-SHA256 签名流程
    #    * 实现规范请求字符串构建
    #    * 实现 HMAC-SHA256 多层签名计算
    #    * 支持动态时间戳和凭证范围
    #    * 自动生成 Authorization header
    #
    #  技术要点 (Technical Notes):
    #  - 使用 UTC 时间戳确保时区一致性
    #  - 签名顺序: SecretKey -> Date -> Service -> "tc3_request"
    #  - 支持的 Service: "hunyuan"
    #  - 支持的 Region: "ap-guangzhou" (默认)
    # ==========================================================================
    def _build_tencent_signature_v3(self, secret_id: str, secret_key: str, service: str, region: str,
                                   action: str, version: str, payload: str, host: str) -> Tuple[str, str]:
        """构建腾讯云 API 签名方法 v3

        Args:
            secret_id: 腾讯云 SecretId
            secret_key: 腾讯云 SecretKey
            service: 服务名称 (hunyuan)
            region: 地域 (ap-guangzhou)
            action: API 动作 (ChatCompletions)
            version: API 版本 (2023-09-01)
            payload: 请求 payload 的 JSON 字符串

        Returns:
            Tuple[str, str]: (authorization_header, timestamp)
        """

        # 1. 创建规范请求字符串
        algorithm = "TC3-HMAC-SHA256"
        timestamp = int(time.time())
        date = datetime.utcfromtimestamp(timestamp).strftime('%Y-%m-%d')  # 腾讯云签名要求 YYYY-MM-DD 格式

        # 规范请求
        canonical_request = self._build_canonical_request(action, payload, host)

        # 2. 创建待签字符串
        credential_scope = f"{date}/{service}/tc3_request"
        string_to_sign = f"{algorithm}\n{timestamp}\n{credential_scope}\n{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"

        # 3. 计算签名
        secret_date = hmac.new(f"TC3{secret_key}".encode('utf-8'), date.encode('utf-8'), hashlib.sha256).digest()
        secret_service = hmac.new(secret_date, service.encode('utf-8'), hashlib.sha256).digest()
        secret_signing = hmac.new(secret_service, "tc3_request".encode('utf-8'), hashlib.sha256).digest()
        signature = hmac.new(secret_signing, string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()

        # 4. 构建 Authorization
        authorization = f"{algorithm} Credential={secret_id}/{credential_scope}, SignedHeaders=content-type;host, Signature={signature}"

        return authorization, str(timestamp)

    def _build_canonical_request(self, action: str, payload: str, host: str) -> str:
        """构建规范请求字符串"""
        # HTTP 请求方法
        http_request_method = "POST"
        # 规范 URI
        canonical_uri = "/"
        # 规范查询字符串
        canonical_querystring = ""
        # 规范头部
        canonical_headers = f"content-type:application/json\nhost:{host}\n"
        # 签名的头部列表
        signed_headers = "content-type;host"
        # 请求载荷的哈希值
        hashed_request_payload = hashlib.sha256(payload.encode('utf-8')).hexdigest()

        canonical_request = f"{http_request_method}\n{canonical_uri}\n{canonical_querystring}\n{canonical_headers}\n{signed_headers}\n{hashed_request_payload}"

        return canonical_request

    # 新增: 设置当前题目索引的方法
    def set_current_question(self, index: int):
        self.current_question_index = index

    def call_first_api(self, img_str: str, prompt: str, ocr_text: str = "") -> Tuple[Optional[str], Optional[str]]:
        return self._call_api_by_group("first", img_str, prompt, ocr_text)

    def call_second_api(self, img_str: str, prompt: str, ocr_text: str = "") -> Tuple[Optional[str], Optional[str]]:
        return self._call_api_by_group("second", img_str, prompt, ocr_text)

    def _call_api_by_group(self, api_group: str, img_str: str, prompt: str, ocr_text: str = "") -> Tuple[Optional[str], Optional[str]]:
        """根据API组别调用对应的预设供应商API"""
        try:
            if api_group == "first":
                provider = self.config_manager.first_api_provider
                api_key = self.config_manager.first_api_key
                model_id = self.config_manager.first_modelID
            elif api_group == "second":
                provider = self.config_manager.second_api_provider
                api_key = self.config_manager.second_api_key
                model_id = self.config_manager.second_modelID
            else:
                return None, "无效的API组别"

            if not all([provider, api_key, model_id]):
                return None, f"第{api_group}组API配置不完整 (供应商、Key或模型ID为空)"

            print(f"[API] 准备调用 {api_group} API, 供应商: {provider}")
            return self._execute_api_call(provider, api_key, model_id, img_str, prompt, ocr_text)
        except Exception as e:
            error_detail = traceback.format_exc()
            print(f"[API] 调用 {api_group} API 时发生严重错误: {str(e)}\n{error_detail}")
            return None, f"API调用失败: {str(e)}"

    def test_api_connection(self, api_group: str) -> Tuple[bool, str]:
        """测试指定API组的连接
        
        包括：
        1. AI评分模型API连接测试
        2. 百度智能云OCR API连接测试（可选）
        """
        try:
            if api_group == "first":
                provider, api_key, model_id, group_name = (
                    self.config_manager.first_api_provider, self.config_manager.first_api_key,
                    self.config_manager.first_modelID, "第一个"
                )
            elif api_group == "second":
                provider, api_key, model_id, group_name = (
                    self.config_manager.second_api_provider, self.config_manager.second_api_key,
                    self.config_manager.second_modelID, "第二个"
                )
            else:
                return False, "无效的API组别"
            
            if not all([provider, api_key.strip(), model_id.strip()]):
                return False, f"{group_name}API配置不完整"

            # 测试AI评分API
            print(f"[API Test] 测试{group_name}API, 供应商: {provider}")
            result, error = self._execute_api_call(provider, api_key, model_id, img_str="", prompt="你好")

            provider_name = PROVIDER_CONFIGS.get(provider, {}).get("name", provider)
            
            if not (result and not error):
                enhanced_error = f"❌ {provider_name}: {error}"
                suggestion = "\n\n💡 请检查API Key、模型ID是否正确，并确保账户有充足余额"
                return False, enhanced_error + suggestion
            
            # AI评分API连接成功，构建结果信息
            result_info = f"✓ {provider_name}: 连接成功"
            
            # 测试百度智能云OCR连接（可选）
            baidu_ocr_info = self._test_baidu_ocr_connection()
            if baidu_ocr_info:
                result_info += f"\n\n{baidu_ocr_info}"
            
            return True, result_info
        except Exception as e:
            error_detail = traceback.format_exc()
            print(f"[API Test] API测试过程中发生异常: {str(e)}\n{error_detail}")
            return False, f"API测试异常: {str(e)}"

    def _test_baidu_ocr_connection(self) -> str:
        """测试百度智能云OCR连接
        
        Returns:
            str: 百度OCR测试结果信息，如果未配置则返回提示信息
        """
        baidu_api_key = self.config_manager.baidu_ocr_api_key
        baidu_secret_key = self.config_manager.baidu_ocr_secret_key
        
        # 检查是否配置了百度OCR信息
        if not baidu_api_key or not baidu_secret_key:
            return "📌 百度智能云OCR：未配置"
        
        # 配置已填写，进行连接测试
        try:
            print(f"[API Test] 测试百度智能云OCR连接")
            result, error = self._execute_api_call("baidu_ocr", baidu_api_key, "", img_str="", prompt="")
            
            if result and not error:
                return "✓ 百度智能云OCR：连接成功"
            else:
                return f"❌ 百度智能云OCR：{error}\n💡 请检查API Key和Secret Key是否正确，且账户有充足余额"
        except Exception as e:
            error_detail = traceback.format_exc()
            print(f"[API Test] 百度OCR测试异常: {str(e)}\n{error_detail}")
            return f"❌ 百度智能云OCR：{str(e)}"

    def _preprocess_api_key(self, api_key: str, auth_method: str) -> Tuple[str, Optional[str]]:
        """
        预处理API Key，增强格式验证和兼容性

        Args:
            api_key: 原始API Key
            auth_method: 鉴权方法

        Returns:
            tuple: (processed_key, error_message)
        """
        if not api_key or not api_key.strip():
            return "", "API Key不能为空"

        api_key = api_key.strip()

        if auth_method == "bearer":
            # 处理Bearer token的重复前缀问题
            if api_key.lower().startswith("bearer "):
                api_key = api_key[7:].strip()  # 移除"Bearer "前缀
            return api_key, None

        elif auth_method == "tencent_signature_v3":
            # 处理腾讯API Key格式
            # 支持中文冒号自动转换
            api_key = api_key.replace("：", ":")  # 中文冒号转英文冒号

            # 检查冒号数量
            colon_count = api_key.count(":")
            if colon_count == 0:
                return "", "腾讯API Key格式错误：缺少冒号分隔符，应为 'SecretId:SecretKey' 格式"
            elif colon_count > 1:
                return "", "腾讯API Key格式错误：冒号数量过多，应为 'SecretId:SecretKey' 格式"

            # 分离SecretId和SecretKey
            parts = api_key.split(":", 1)
            secret_id, secret_key = parts[0].strip(), parts[1].strip()

            # 验证格式合理性
            if not secret_id:
                return "", "腾讯API Key格式错误：SecretId不能为空"
            if not secret_key:
                return "", "腾讯API Key格式错误：SecretKey不能为空"
            if len(secret_id) < 10:
                return "", "腾讯API Key格式错误：SecretId长度过短"
            if len(secret_key) < 10:
                return "", "腾讯API Key格式错误：SecretKey长度过短"

            return f"{secret_id}:{secret_key}", None

        elif auth_method == "google_api_key_in_url":
            # Google Gemini API Key - 直接使用，无特殊格式要求
            # API Key会被添加到URL参数中，不需要特殊处理
            if len(api_key) < 20:  # 基本长度检查
                return "", "Google API Key格式错误：Key长度过短"
            return api_key, None

        elif auth_method == "baidu_ocr_token":
            # 百度OCR API Key - 直接使用
            # 这是API Key部分，Secret Key在配置中单独存储
            if len(api_key) < 10:
                return "", "百度OCR API Key格式错误：Key长度过短"
            return api_key, None

        # 其他鉴权方法直接返回
        return api_key, None

    def _execute_api_call(self, provider: str, api_key: str, model_id: str, img_str: str, prompt: str, ocr_text: str = "") -> Tuple[Optional[str], Optional[str]]:
        # 在函数开始就获取provider_name，避免异常处理时未定义
        provider_name = PROVIDER_CONFIGS.get(provider, {}).get("name", provider)
        
        if provider not in PROVIDER_CONFIGS:
            return None, f"未知的供应商标识: {provider}"

        config = PROVIDER_CONFIGS[provider]
        url = config["url"]
        
        # 支持动态URL（例如Gemini需要在URL中包含模型名称）
        if config.get("dynamic_url", False):
            url = url.replace("{model}", model_id)
        
        headers = {}
        use_json_format = True  # 默认使用JSON格式
        auth_method = config.get("auth_method", "bearer")

        # 预处理API Key
        processed_key, key_error = self._preprocess_api_key(api_key, auth_method)
        if key_error:
            return None, key_error

        # 如果有OCR文本，将其添加到prompt中
        enhanced_prompt = prompt
        if ocr_text and ocr_text.strip():
            enhanced_prompt = f"OCR识别的文字内容：\n{ocr_text.strip()}\n\n{prompt}"

        # 防御性检查: 不允许在一次API调用中同时提供图像和OCR文本作为双重输入
        if img_str and isinstance(img_str, str) and img_str.strip() and ocr_text and isinstance(ocr_text, str) and ocr_text.strip():
            return None, "禁止同时提供图像和OCR文本作为输入，请选择纯视觉模式或OCR文本模式。"

        # 先构建 payload，因为腾讯签名需要用到它
        try:
            builder_func = getattr(self, config["payload_builder"])
            payload = builder_func(model_id, img_str, enhanced_prompt)
        except Exception as e:
            return None, f"构建请求体失败: {e}"

        # 特殊处理百度OCR：使用form-data格式
        if provider == "baidu_ocr":
            use_json_format = False
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        # 鉴权处理
        if auth_method == "bearer":
            headers["Authorization"] = f"Bearer {processed_key}"
        elif auth_method == "google_api_key_in_url": # For Gemini
             url += f"?key={processed_key}"
        elif auth_method == "tencent_signature_v3":
            # 腾讯云签名方法 v3 - 使用预处理后的Key
            secret_id, secret_key = processed_key.split(":", 1)
            payload_str = json.dumps(payload, separators=(',', ':'))

            # 从配置中读取服务信息，避免硬编码
            service_info = config.get("service_info", {})
            service = service_info.get("service", "hunyuan")
            region = service_info.get("region", "ap-guangzhou")
            version = service_info.get("version", "2023-09-01")
            action = service_info.get("action", "ChatCompletions")

            host = service_info.get("host", "hunyuan.tencentcloudapi.com")
            authorization, timestamp = self._build_tencent_signature_v3(
                secret_id, secret_key, service, region, action, version, payload_str, host
            )
            headers["Authorization"] = authorization
            headers["X-TC-Timestamp"] = timestamp
            headers["X-TC-Version"] = version
            headers["X-TC-Action"] = action
            headers["X-TC-Region"] = region
        elif auth_method == "baidu_ocr_token":
            # 百度OCR Token鉴权 - 基于用户教程
            # 获取Access Token
            token_url = "https://aip.baidubce.com/oauth/2.0/token"
            token_params = {
                "grant_type": "client_credentials",
                "client_id": processed_key,  # API Key
                "client_secret": self.config_manager.baidu_ocr_secret_key  # Secret Key
            }

            try:
                self.logger.debug("准备获取百度OCR Access Token")
                token_response = self.session.post(token_url, data=token_params, timeout=10)
                token_data = token_response.json()

                if "access_token" in token_data:
                    access_token = token_data["access_token"]
                    # 添加到URL参数中
                    url += f"?access_token={access_token}"
                    self.logger.debug("百度OCR Access Token 获取成功")
                else:
                    self.logger.warning(f"百度OCR Token返回结果不包含access_token: {token_data}")
                    return None, f"获取百度OCR Access Token失败: {token_data}"
            except Exception as e:
                self.logger.exception("百度OCR Token获取异常")
                return None, f"百度OCR Token获取异常: {str(e)}"

        # 通用请求发送逻辑（所有认证方式共享）
        try:
            self.logger.debug(f"[{provider_name}] 发送API请求到: {url}")
            
            # 根据格式选择传递方式
            if use_json_format:
                headers["Content-Type"] = "application/json"
                response = self.session.post(url, headers=headers, json=payload, timeout=60)
            else:
                # form-data格式（用于百度OCR等）
                response = self.session.post(url, headers=headers, data=payload, timeout=60)

            self.logger.debug(f"[{provider_name}] 收到响应: 状态码 {response.status_code}")
            
            if response.status_code == 200:
                content = self._extract_response_content(response.json(), provider)
                if content:
                    self.logger.debug(f"[{provider_name}] 成功提取响应内容")
                    return content, None
                else:
                    self.logger.warning(f"[{provider_name}] 响应内容为空或无法解析")
                    return None, f"API响应内容为空或无法解析。原始响应: {str(response.json())[:200]}"
            else:
                error_text = response.text[:200]
                self.logger.warning(f"[{provider_name}] API请求失败: {response.status_code}")
                friendly_error = self._create_api_error_message(provider, response.status_code, error_text)
                return None, friendly_error
        except requests.exceptions.Timeout:
            self.logger.warning(f"[{provider_name}] 请求超时")
            return None, f"[{provider_name}] 请求超时，请检查网络连接或稍后重试"
        except requests.exceptions.ConnectionError as e:
            self.logger.warning(f"[{provider_name}] 连接失败: {str(e)[:100]}")
            return None, f"[{provider_name}] 无法连接到服务器，请检查网络设置"
        except requests.exceptions.RequestException as e:
            self.logger.exception(f"[{provider_name}] 网络请求异常")
            friendly_error = self._create_network_error_message(e)
            return None, friendly_error

    def _extract_response_content(self, data: Dict[str, Any], provider: str) -> Optional[str]:
        """从API响应中提取内容
        
        支持的提供商响应格式：
        - OpenAI兼容格式: openai, moonshot, openrouter, zhipu, volcengine, aliyun, baidu
        - 腾讯混元格式: tencent
        - Google Gemini格式: gemini
        - 百度OCR格式: baidu_ocr
        """
        try:
            # OpenAI兼容格式 - 标准的 choices[0].message.content
            if provider in ["openai", "moonshot", "openrouter", "zhipu", "volcengine", "aliyun", "baidu"]:
                return data["choices"][0]["message"]["content"]
            
            # 腾讯混元 - 使用相同的OpenAI兼容格式
            if provider == "tencent":
                return data["choices"][0]["message"]["content"]
            
            # Google Gemini - 特殊格式
            if provider == "gemini":
                return data["candidates"][0]["content"]["parts"][0]["text"]
            
            # 百度OCR - 特殊的OCR响应格式
            if provider == "baidu_ocr":
                # 百度OCR响应格式处理 - 支持handwriting和doc_analysis格式
                # Return a plain text merged result for legacy flows
                if "results" in data and data["results"]:
                    # handwriting API格式：results -> words -> word
                    words = []
                    for result in data["results"]:
                        if "words" in result and isinstance(result["words"], dict):
                            word_text = result["words"].get("word", "")
                            if word_text:
                                # 可选：添加置信度信息（如果有）
                                if "probability" in result["words"] and "average" in result["words"]["probability"]:
                                    confidence = result["words"]["probability"]["average"]
                                    if confidence < 0.8:  # 置信度低于80%标记
                                        word_text += f" (低置信度:{confidence:.2f})"
                                words.append(word_text)
                    if words:
                        return "\n".join(words)
                    else:
                        return "OCR未能识别到文字"
                elif "words_result" in data and data["words_result"]:
                    # doc_analysis API格式：words_result数组
                    words = []
                    for item in data["words_result"]:
                        if "words" in item:
                            word_text = item["words"]
                            # 可选：添加置信度信息
                            if "probability" in item and "average" in item["probability"]:
                                confidence = item["probability"]["average"]
                                if confidence < 0.8:  # 置信度低于80%标记
                                    word_text += f" (低置信度:{confidence:.2f})"
                            words.append(word_text)
                    if words:
                        return "\n".join(words)
                    else:
                        return "OCR未能识别到文字"
                else:
                    return "OCR未能识别到文字"
        except (KeyError, IndexError, TypeError) as e:
            print(f"解析{provider}响应失败: {e}")
            return None # 解析失败
        return str(data) # Fallback

    # 新: 专用方法用于获取百度 doc_analysis 的原始结构化结果（便于置信度分析）
    def call_baidu_doc_analysis_structured(self, img_str: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        调用百度doc_analysis接口并返回未被处理的JSON结构，便于分析每行置信度等信息。
        Returns: (data_dict, error_message)
        """
        try:
            # token 获取
            token_url = "https://aip.baidubce.com/oauth/2.0/token"
            token_params = {
                "grant_type": "client_credentials",
                "client_id": self.config_manager.baidu_ocr_api_key,
                "client_secret": self.config_manager.baidu_ocr_secret_key
            }
            token_response = self.session.post(token_url, data=token_params, timeout=10)
            token_data = token_response.json()
            if "access_token" not in token_data:
                return None, f"获取百度OCR access_token失败: {token_data}"
            access_token = token_data["access_token"]

            # doc_analysis endpoint
            url = "https://aip.baidubce.com/rest/2.0/ocr/v1/doc_analysis"
            url += f"?access_token={access_token}"

            pure_base64 = self._get_pure_base64(img_str)
            # 🎯 优化后的参数配置（v2.0 - 2025-12-12）
            # - 输入：经过预处理的手写答案图像（灰度化+二值化+去噪）
            # - 目标：准确识别手写内容，忽略涂改部分
            # - 策略：纯文本识别+质量检测，质量差时人工介入
            # - 优化：移除无效参数，减少API调用开销
            payload = {
                "image": pure_base64,
                "language_type": "CHN_ENG",      # 中英文混合
                "result_type": "big",             # 行级结果（已足够精确）
                "words_type": "handprint_mix",   # 明确指定手写印刷混排模式
                "line_probability": True,         # ✅ 必需：置信度检测
                "recg_alter": True,               # ✅ 必需：涂改检测（用于完全忽略涂改行）
                # ✅ 优化说明：已移除无效参数
                # - detect_direction: 答题卡方向已固定，无需检测
                # - detect_language: 已明确指定CHN_ENG，无需额外检测
                # - layout_analysis: 已框定答案区域，不需要版面分析
                # - recg_formula/recg_long_division: 暂不使用特殊格式识别
            }

            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            response = self.session.post(url, headers=headers, data=payload, timeout=30)
            if response.status_code != 200:
                return None, f"百度DocAnalysis请求失败: {response.status_code} {response.text[:200]}"
            return response.json(), None
        except Exception as e:
            self.logger.exception("调用百度文档分析接口异常")
            return None, f"调用百度文档分析接口异常: {str(e)}"

    def _get_pure_base64(self, img_str: str) -> str:
        if not img_str: return ""
        marker = "base64,"
        pos = img_str.find(marker)
        return img_str[pos + len(marker):] if pos != -1 else img_str

    # ==========================================================================
    #  各厂商专属的Payload构建函数
    # ==========================================================================
    def _build_openai_compatible_payload(self, model_id, img_str, prompt):
        """
        适用于大多数与OpenAI兼容的厂商 (Moonshot, 智谱, Baidu V2, Aliyun-Compatible等)
        核心原则: 图片在前，文本在后，以保证最大兼容性。
        """
        if not img_str:
            return {"model": model_id, "messages": [{"role": "user", "content": prompt}], "max_tokens": 4096}

        pure_base64 = self._get_pure_base64(img_str)
        return {
            "model": model_id,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{pure_base64}"}},
                {"type": "text", "text": prompt}
            ]}],
            "max_tokens": 4096
        }



    def _build_volcengine_payload(self, model_id, img_str, prompt):
        """
        专为火山引擎定制 - 符合官方API文档格式

        AI自动改卷程序专用优化 (2025-09-13 更新):
        ============================================
        当前优化: 默认使用高细节模式提升手写文字识别精度
        适用场景: AI批改学生答案图片，需准确识别手写内容

        优化详情:
        - detail: "high" - 高细节模式，适用于复杂手写识别
        - 优势: 更好的文字识别精度，适合教育场景
        - 权衡: 可能增加响应时间和token消耗

        后续优化计划:
        ============================================
        1. 图片质量自适应: 根据图片复杂度自动选择detail等级
        2. 模型验证: 确保用户选择的模型支持视觉输入
        3. 性能监控: 添加图片大小和处理时间统计
        4. 配置选项: 允许用户自定义detail参数
        5. 批量优化: 支持多图片同时处理
        """
        if not img_str:
            # 纯文本模式 - 不涉及图片时使用简单格式
            return {
                "model": model_id,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 4096
            }

        # 视觉模式 - AI改卷专用配置
        # 按照火山引擎官方文档：image在前，text在后
        pure_base64 = self._get_pure_base64(img_str)
        return {
            "model": model_id,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{pure_base64}",
                            "detail": "high"  # 高细节模式 - 优化手写文字识别
                        }
                    },
                    {
                        "type": "text",
                        "text": prompt
                    }
                ]
            }],
            "max_tokens": 4096
        }





    def _build_tencent_payload(self, model_id, img_str, prompt):
        """专为腾讯混元定制 - 支持所有视觉模型

        更新历史 (Update History):
        - 2025-09-13: 重构 payload 构建逻辑
          * 统一使用 ChatCompletions 接口格式
          * 实现智能视觉模型检测
          * 支持动态模型名称输入
          * 自动选择 Contents vs Content 格式

        支持的视觉模型包括：
        - hunyuan-vision (基础多模态)
        - hunyuan-turbos-vision (旗舰模型)
        - hunyuan-turbos-vision-20250619 (最新旗舰)
        - hunyuan-t1-vision (深度思考)
        - hunyuan-t1-vision-20250619 (最新深度思考)
        - hunyuan-large-vision (多语言支持)

        未来维护注意事项 (Future Maintenance Notes):
        - 如果新模型名称不含 "vision"，需要更新检测逻辑
        - 如果腾讯改变 payload 格式，需要相应调整
        - 支持的图像格式：JPEG (base64编码)
        - 图像URL格式：data:image/jpeg;base64,{base64_data}

        Args:
            model_id: 模型名称，由用户界面输入
            img_str: 图像base64字符串（可选）
            prompt: 文本提示

        Returns:
            dict: 符合腾讯API格式的请求payload
        """
        # 腾讯所有视觉模型都支持图像输入，通过模型名中的 "vision" 标识
        is_vision_model = "vision" in model_id.lower()

        if not img_str or not is_vision_model:
            # 纯文本模式或非视觉模型
            return {
                "Model": model_id,
                "Messages": [{"Role": "user", "Content": prompt}],
                "Stream": False
            }

        # 视觉模型支持图像输入
        pure_base64 = self._get_pure_base64(img_str)
        return {
            "Model": model_id,
            "Messages": [{
                "Role": "user",
                "Contents": [
                    {"Type": "text", "Text": prompt},
                    {"Type": "image_url", "ImageUrl": {"Url": f"data:image/jpeg;base64,{pure_base64}"}}
                ]
            }],
            "Stream": False
        }



    def _build_gemini_payload(self, model_id, img_str, prompt):
        """专为 Google Gemini 定制"""
        if not img_str:
             return {"contents": [{"parts": [{"text": prompt}]}]}

        pure_base64 = self._get_pure_base64(img_str)
        return {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": "image/jpeg", "data": pure_base64}}
                ]
            }]
        }

    def _build_baidu_ocr_payload(self, model_id, img_str, prompt):
        """专为百度OCR定制 - 手写文字识别

        基于用户提供的百度OCR教程实现：
        - 使用手写文字识别API端点
        - 参数通过form data传递
        - 支持多种配置选项
        """
        # 百度OCR不使用model_id参数，而是使用固定的API端点
        # prompt参数在这里不使用，因为OCR主要关注图片内容
        if not img_str:
            return {}

        pure_base64 = self._get_pure_base64(img_str)
        return {
            "image": pure_base64,
            "language_type": "CHN_ENG",  # 中英文混合
            "detect_direction": "true",   # 检测图像朝向
            "detect_language": "true",    # 检测语言
            "probability": "true"        # 返回识别结果中每一行的置信度
        }

    def _create_api_error_message(self, provider: str, status_code: int, response_text: str) -> str:
        """根据API返回的错误，生成对用户更友好的错误信息。"""
        provider_name = PROVIDER_CONFIGS.get(provider, {}).get("name", provider)

        if status_code == 401 or status_code == 403:
            return (f"【认证失败】{provider_name} 的 API Key 无效或已过期。\n"
                    f"解决方案：请前往 {provider_name} 官网，检查并重新复制粘贴您的 API Key。")

        if status_code == 400:
            if "zhipu" in provider and "1210" in response_text:
                return (f"【参数错误】发送给 {provider_name} 的模型ID可能有误。\n"
                        f"解决方案：请检查您为 {provider_name} 设置的模型ID是否正确、可用，且您的账户有权访问。")
            else:
                return (f"【请求错误】发送给 {provider_name} 的请求参数有误。\n"
                        f"常见原因：模型ID填写错误或不兼容。请核对后重试。")

        if status_code == 429:
            return (f"【请求超限】您对 {provider_name} 的API请求过于频繁，已触发限流。\n"
                    f"解决方案：请稍等片刻再试，或在程序中增大'等待时间'。")

        # 返回一个通用的、但更清晰的错误
        return (f"【服务异常】{provider_name} 服务器返回了未处理的错误 (状态码: {status_code})。\n"
                f"服务器响应(部分): {response_text[:100]}")

    def _create_network_error_message(self, error: requests.exceptions.RequestException) -> str:
        """根据网络异常类型，生成用户友好的信息"""
        error_str = str(error)
        if "Invalid leading whitespace" in error_str:
            return ("【格式错误】您的 API Key 中可能包含了非法字符（如换行或多余的文字）。\n"
                    "解决方案：请彻底清空API Key输入框，然后从官网【精确地】只复制Key本身，再粘贴回来。")

        if "timed out" in error_str.lower():
            return ("【网络超时】连接API服务器超时。\n"
                    "解决方案：请检查您的网络连接是否通畅，或稍后再试。")

        # 通用网络错误
        return f"【网络连接失败】无法连接到API服务器。\n请检查您的网络设置和防火墙。错误详情: {error_str[:150]}"

    def update_config_from_manager(self):
        """
        这个方法在我们的新架构中不再需要。
        因为 `call_api` 等方法每次都会直接从 `config_manager` 读取最新的配置。
        保留此空方法以防止旧代码调用时出错。
        """
        pass

    def validate_provider_configuration(self) -> Dict[str, Any]:
        """
        验证所有配置的API提供商是否有完整的实现
        
        Returns:
            Dict: 验证结果，包含每个提供商的实现状态
        """
        validation_results = {}
        
        for provider_id, config in PROVIDER_CONFIGS.items():
            result = {
                "provider_id": provider_id,
                "name": config.get("name", "未命名"),
                "has_url": bool(config.get("url")),
                "has_auth_method": bool(config.get("auth_method")),
                "has_payload_builder": bool(config.get("payload_builder")),
                "payload_builder_exists": False,
                "response_parser_exists": False,
                "is_complete": False
            }
            
            # 检查payload构建器是否存在
            builder_name = config.get("payload_builder", "")
            if builder_name and hasattr(self, builder_name):
                result["payload_builder_exists"] = True
            
            # 检查响应解析器是否支持该提供商
            # 通过检查 _extract_response_content 中是否有该provider的处理
            supported_providers = [
                "openai", "moonshot", "openrouter", "zhipu", "volcengine", 
                "aliyun", "baidu", "tencent", "gemini", "baidu_ocr"
            ]
            result["response_parser_exists"] = provider_id in supported_providers
            
            # 判断是否完整
            result["is_complete"] = (
                result["has_url"] and 
                result["has_auth_method"] and 
                result["has_payload_builder"] and 
                result["payload_builder_exists"] and 
                result["response_parser_exists"]
            )
            
            validation_results[provider_id] = result
        
        return validation_results

# ==============================================================================
#  配置验证和诊断函数 (Configuration Validation and Diagnostics)
# ==============================================================================
def validate_all_providers() -> None:
    """
    验证所有API提供商的配置完整性
    用于开发和调试目的
    """
    print("=" * 80)
    print("API提供商配置验证报告")
    print("=" * 80)
    
    # 创建临时实例进行验证
    class MockConfigManager:
        """模拟配置管理器用于验证"""
        pass
    
    service = ApiService(MockConfigManager())
    results = service.validate_provider_configuration()
    
    complete_count = 0
    incomplete_count = 0
    
    for provider_id, result in results.items():
        status = "✅ 完整" if result["is_complete"] else "❌ 不完整"
        print(f"\n{status} [{provider_id}] {result['name']}")
        
        if result["is_complete"]:
            complete_count += 1
        else:
            incomplete_count += 1
            # 显示缺失的部分
            issues = []
            if not result["has_url"]:
                issues.append("缺少URL配置")
            if not result["has_auth_method"]:
                issues.append("缺少认证方法")
            if not result["has_payload_builder"]:
                issues.append("缺少payload构建器配置")
            if not result["payload_builder_exists"]:
                issues.append("payload构建器未实现")
            if not result["response_parser_exists"]:
                issues.append("响应解析器未实现")
            
            print(f"   问题: {', '.join(issues)}")
    
    print("\n" + "=" * 80)
    print(f"验证摘要: 完整 {complete_count} 个, 不完整 {incomplete_count} 个")
    print("=" * 80)

# 如果直接运行此文件，执行验证
if __name__ == "__main__":
    validate_all_providers()

# --- END OF FILE api_service.py ---
