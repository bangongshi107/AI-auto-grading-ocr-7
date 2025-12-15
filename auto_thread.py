import os
import time
import base64
import traceback
import pyautogui
import datetime
from io import BytesIO
from PIL import ImageGrab, Image
from PyQt5.QtCore import QThread, pyqtSignal
import math
import json
import re
import random
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Callable, Any, Tuple
from threading import Lock
from functools import wraps
from enum import Enum

# 导入OCR配置函数
from config_manager import get_ocr_quality_internal_value


# ==================== 自定义异常层次结构 ====================

class GradingError(Exception):
    """阅卷系统基础异常类
    
    所有自定义异常的基类，提供统一的错误信息格式和恢复建议。
    """
    
    def __init__(self, message: str, recoverable: bool = False, 
                 recovery_action: str = "", original_error: Optional[Exception] = None):
        """
        Args:
            message: 错误描述信息
            recoverable: 是否可自动恢复
            recovery_action: 建议的恢复操作
            original_error: 原始异常（用于异常链）
        """
        super().__init__(message)
        self.message = message
        self.recoverable = recoverable
        self.recovery_action = recovery_action
        self.original_error = original_error
    
    def __str__(self):
        base = self.message
        if self.recovery_action:
            base += f" [建议操作: {self.recovery_action}]"
        return base


class ConfigError(GradingError):
    """配置相关错误
    
    包括：配置文件缺失/格式错误、必需参数未设置、参数值无效等。
    通常需要用户修改配置后重试。
    """
    
    def __init__(self, message: str, config_key: str = "", 
                 expected_type: str = "", original_error: Optional[Exception] = None):
        recovery = "请检查配置文件或在设置界面修正配置"
        if config_key:
            recovery = f"请检查配置项 '{config_key}'"
            if expected_type:
                recovery += f"，期望类型: {expected_type}"
        super().__init__(message, recoverable=False, 
                        recovery_action=recovery, original_error=original_error)
        self.config_key = config_key
        self.expected_type = expected_type


class NetworkError(GradingError):
    """网络相关错误
    
    包括：连接超时、网络不可达、API服务不可用、限流等。
    通常可以通过重试恢复。
    """
    
    # 网络错误子类型
    TYPE_TIMEOUT = "timeout"           # 连接/读取超时
    TYPE_CONNECTION = "connection"     # 连接失败
    TYPE_RATE_LIMIT = "rate_limit"     # API限流（429）
    TYPE_SERVICE_DOWN = "service_down" # 服务不可用（503）
    TYPE_SERVER_ERROR = "server_error" # 服务器内部错误（5xx）
    
    def __init__(self, message: str, error_type: str = "", 
                 retry_after: int = 0, original_error: Optional[Exception] = None):
        # 根据错误类型设置恢复建议
        recovery_map = {
            self.TYPE_TIMEOUT: "请检查网络连接，稍后重试",
            self.TYPE_CONNECTION: "请检查网络连接和API地址配置",
            self.TYPE_RATE_LIMIT: f"API请求过于频繁，请等待{retry_after}秒后重试" if retry_after else "API请求过于频繁，请稍后重试",
            self.TYPE_SERVICE_DOWN: "API服务暂时不可用，请稍后重试",
            self.TYPE_SERVER_ERROR: "API服务器错误，请稍后重试",
        }
        recovery = recovery_map.get(error_type, "请检查网络连接后重试")
        
        # 网络错误通常可重试
        super().__init__(message, recoverable=True, 
                        recovery_action=recovery, original_error=original_error)
        self.error_type = error_type
        self.retry_after = retry_after


class BusinessError(GradingError):
    """业务逻辑错误
    
    包括：评分解析失败、分数超出范围、OCR识别失败、答案区域无效等。
    根据具体情况可能需要人工介入或可以自动恢复。
    """
    
    # 业务错误子类型
    TYPE_SCORE_PARSE = "score_parse"       # 分数解析失败
    TYPE_SCORE_RANGE = "score_range"       # 分数超出范围
    TYPE_OCR_FAILURE = "ocr_failure"       # OCR识别失败
    TYPE_AREA_INVALID = "area_invalid"     # 答案区域无效
    TYPE_API_RESPONSE = "api_response"     # API响应格式错误
    TYPE_DUAL_EVAL = "dual_eval"           # 双评分差超阈值
    
    def __init__(self, message: str, error_type: str = "", 
                 question_index: int = 0, recoverable: bool = False,
                 original_error: Optional[Exception] = None):
        # 根据错误类型设置恢复建议
        recovery_map = {
            self.TYPE_SCORE_PARSE: "AI返回的分数格式无效，请检查Prompt设置或手动评分",
            self.TYPE_SCORE_RANGE: "分数已自动修正到有效范围",
            self.TYPE_OCR_FAILURE: "OCR识别失败，请检查图像质量或切换为纯AI模式",
            self.TYPE_AREA_INVALID: "请重新配置答案区域",
            self.TYPE_API_RESPONSE: "API响应格式异常，可能需要更换模型",
            self.TYPE_DUAL_EVAL: "双评分差超过阈值，需要人工复核",
        }
        recovery = recovery_map.get(error_type, "请检查相关配置或手动处理")
        
        super().__init__(message, recoverable=recoverable, 
                        recovery_action=recovery, original_error=original_error)
        self.error_type = error_type
        self.question_index = question_index


class ResourceError(GradingError):
    """资源相关错误
    
    包括：文件读写失败、内存不足、截图失败等系统资源问题。
    """
    
    TYPE_FILE_IO = "file_io"           # 文件读写错误
    TYPE_SCREENSHOT = "screenshot"     # 截图失败
    TYPE_MEMORY = "memory"             # 内存不足
    
    def __init__(self, message: str, error_type: str = "",
                 resource_path: str = "", original_error: Optional[Exception] = None):
        recovery_map = {
            self.TYPE_FILE_IO: f"文件操作失败: {resource_path}" if resource_path else "文件操作失败，请检查权限",
            self.TYPE_SCREENSHOT: "截图失败，请检查屏幕访问权限",
            self.TYPE_MEMORY: "内存不足，请关闭其他程序后重试",
        }
        recovery = recovery_map.get(error_type, "请检查系统资源")
        
        super().__init__(message, recoverable=False,
                        recovery_action=recovery, original_error=original_error)
        self.error_type = error_type
        self.resource_path = resource_path


# ==================== 异常恢复策略管理器 ====================

class ErrorRecoveryManager:
    """异常恢复策略管理器
    
    根据不同类型的异常提供相应的恢复策略和建议。
    """
    
    @staticmethod
    def classify_exception(error: Exception) -> GradingError:
        """将标准异常转换为自定义异常类型
        
        Args:
            error: 原始异常
            
        Returns:
            对应的GradingError子类实例
        """
        error_str = str(error).lower()
        
        # 检测网络相关错误
        if any(kw in error_str for kw in ['timeout', '超时', 'timed out']):
            return NetworkError(str(error), NetworkError.TYPE_TIMEOUT, original_error=error)
        
        if any(kw in error_str for kw in ['connection', '连接', 'network', '网络']):
            return NetworkError(str(error), NetworkError.TYPE_CONNECTION, original_error=error)
        
        if any(kw in error_str for kw in ['429', 'rate limit', '限流', 'too many']):
            return NetworkError(str(error), NetworkError.TYPE_RATE_LIMIT, original_error=error)
        
        if any(kw in error_str for kw in ['503', 'service unavailable', '服务不可用']):
            return NetworkError(str(error), NetworkError.TYPE_SERVICE_DOWN, original_error=error)
        
        if any(kw in error_str for kw in ['500', '502', '504', 'internal server']):
            return NetworkError(str(error), NetworkError.TYPE_SERVER_ERROR, original_error=error)
        
        # 检测配置相关错误
        if isinstance(error, KeyError):
            return ConfigError(f"配置字段缺失: {error}", config_key=str(error), original_error=error)
        
        if isinstance(error, ValueError):
            # 尝试区分配置错误和业务错误
            if any(kw in error_str for kw in ['config', '配置', 'parameter', '参数']):
                return ConfigError(str(error), original_error=error)
            else:
                return BusinessError(str(error), BusinessError.TYPE_SCORE_PARSE, original_error=error)
        
        # 检测资源相关错误
        if isinstance(error, (IOError, OSError, FileNotFoundError, PermissionError)):
            return ResourceError(str(error), ResourceError.TYPE_FILE_IO, original_error=error)
        
        if isinstance(error, MemoryError):
            return ResourceError(str(error), ResourceError.TYPE_MEMORY, original_error=error)
        
        # 默认作为业务错误
        return BusinessError(str(error), original_error=error)
    
    @staticmethod
    def get_recovery_strategy(error: GradingError) -> dict:
        """获取错误恢复策略
        
        Args:
            error: GradingError实例
            
        Returns:
            恢复策略字典，包含:
            - should_retry: 是否应该重试
            - retry_delay: 重试延迟（秒）
            - max_retries: 最大重试次数
            - should_stop: 是否应该停止整个流程
            - notify_user: 是否需要通知用户
            - log_level: 日志级别
        """
        strategy = {
            'should_retry': False,
            'retry_delay': 1.0,
            'max_retries': 3,
            'should_stop': True,
            'notify_user': True,
            'log_level': 'ERROR'
        }
        
        if isinstance(error, NetworkError):
            # 网络错误：通常可重试
            strategy['should_retry'] = True
            strategy['should_stop'] = False
            strategy['log_level'] = 'WARNING'
            
            if error.error_type == NetworkError.TYPE_RATE_LIMIT:
                strategy['retry_delay'] = max(error.retry_after, 5.0)
                strategy['max_retries'] = 5
            elif error.error_type == NetworkError.TYPE_TIMEOUT:
                strategy['retry_delay'] = 2.0
                strategy['max_retries'] = 3
            elif error.error_type == NetworkError.TYPE_SERVER_ERROR:
                strategy['retry_delay'] = 3.0
                strategy['max_retries'] = 2
        
        elif isinstance(error, ConfigError):
            # 配置错误：需要停止并通知用户
            strategy['should_retry'] = False
            strategy['should_stop'] = True
            strategy['notify_user'] = True
            strategy['log_level'] = 'ERROR'
        
        elif isinstance(error, BusinessError):
            # 业务错误：根据子类型决定
            if error.error_type == BusinessError.TYPE_SCORE_RANGE:
                # 分数范围错误：已自动修正，可继续
                strategy['should_retry'] = False
                strategy['should_stop'] = False
                strategy['notify_user'] = False
                strategy['log_level'] = 'WARNING'
            elif error.error_type == BusinessError.TYPE_DUAL_EVAL:
                # 双评差异：需要人工介入
                strategy['should_stop'] = True
                strategy['notify_user'] = True
            else:
                # 其他业务错误：停止当前题目
                strategy['should_stop'] = True
                strategy['notify_user'] = True
        
        elif isinstance(error, ResourceError):
            # 资源错误：通常需要停止
            strategy['should_retry'] = False
            strategy['should_stop'] = True
            strategy['notify_user'] = True
            strategy['log_level'] = 'ERROR'
        
        return strategy
    
    @staticmethod
    def format_error_message(error: GradingError, include_recovery: bool = True) -> str:
        """格式化错误消息
        
        Args:
            error: GradingError实例
            include_recovery: 是否包含恢复建议
            
        Returns:
            格式化的错误消息
        """
        # 确定错误类型前缀
        type_prefix = {
            ConfigError: "[配置错误]",
            NetworkError: "[网络错误]",
            BusinessError: "[业务错误]",
            ResourceError: "[资源错误]",
            GradingError: "[系统错误]"
        }
        
        prefix = "[错误]"
        for err_type, pref in type_prefix.items():
            if isinstance(error, err_type):
                prefix = pref
                break
        
        message = f"{prefix} {error.message}"
        
        if include_recovery and error.recovery_action:
            message += f"\n  → 建议: {error.recovery_action}"
        
        return message


# ==================== 分数处理管道类 ====================

class ScoreProcessor:
    """
    统一的分数处理管道类，负责分数的清洗→校验→四舍五入→范围限制。
    确保所有分数处理逻辑集中在一个地方，避免边界情况漏处理。
    """
    
    @staticmethod
    def sanitize(val) -> float:
        """
        清洗和标准化分数输入，确保返回有效的浮点数。
        如果无法提取有效数字，抛出 ValueError 以确保评分准确性。
        
        Args:
            val: 待清洗的分数值（可以是数字、字符串等）
            
        Returns:
            清洗后的浮点数
            
        Raises:
            ValueError: 无法转换为有效分数时
        """
        if isinstance(val, (int, float)):
            return float(val)
        
        # 尝试从字符串中提取数字
        try:
            # 提取浮点数（包括负数）
            match = re.search(r'-?\d+\.?\d*', str(val))
            if match:
                return float(match.group())
        except Exception:
            pass
        
        raise ValueError(f"无法将 {val} 转换为有效的分数")
    
    @staticmethod
    def round_to_step(value: float, step: float) -> float:
        """
        将数值四舍五入到指定步长的倍数。
        
        Args:
            value: 要四舍五入的数值
            step: 步长（如0.5或1）
        
        Returns:
            四舍五入后的值
            
        Examples:
            round_to_step(7.3, 0.5) -> 7.5
            round_to_step(7.3, 1.0) -> 7.0
            round_to_step(7.8, 0.5) -> 8.0
        """
        if step <= 0:
            return value
        return round(value / step) * step
    
    @staticmethod
    def validate_range(score: float, min_score: float, max_score: float, 
                      logger: Optional[Callable] = None) -> float:
        """
        验证分数是否在有效范围内，超出则修正并记录日志。
        
        Args:
            score: 待验证的分数
            min_score: 最低分
            max_score: 最高分
            logger: 可选的日志记录函数，签名为 logger(message, is_error, level)
            
        Returns:
            修正后的分数
        """
        if score < min_score:
            if logger:
                logger(f"分数 {score} 低于最低分 {min_score}，修正为 {min_score}。", True, "ERROR")
            return min_score
        elif score > max_score:
            if logger:
                logger(f"分数 {score} 超出最高分 {max_score}，修正为 {max_score}。", True, "ERROR")
            return max_score
        return score
    
    @classmethod
    def process_pipeline(cls, raw_score, min_score: float, max_score: float, 
                        rounding_step: float = 0.5,
                        logger: Optional[Callable] = None) -> Tuple[float, str]:
        """
        完整的分数处理管道：清洗→四舍五入→范围校验。
        
        Args:
            raw_score: 原始分数（任意类型）
            min_score: 最低分
            max_score: 最高分
            rounding_step: 四舍五入步长（默认0.5）
            logger: 可选的日志记录函数
            
        Returns:
            (处理后的最终分数, 处理过程描述)
            
        Raises:
            ValueError: 无法清洗分数时
        """
        steps_log = []
        
        # 步骤1: 清洗分数
        try:
            sanitized = cls.sanitize(raw_score)
            steps_log.append(f"清洗: {raw_score} → {sanitized}")
        except ValueError as e:
            raise ValueError(f"分数清洗失败: {e}")
        
        # 步骤2: 四舍五入到步长
        rounded = cls.round_to_step(sanitized, rounding_step)
        if rounded != sanitized:
            steps_log.append(f"四舍五入(步长{rounding_step}): {sanitized} → {rounded}")
        
        # 步骤3: 范围校验和修正
        validated = cls.validate_range(rounded, min_score, max_score, logger)
        if validated != rounded:
            steps_log.append(f"范围修正: {rounded} → {validated}")
        
        process_desc = " | ".join(steps_log) if steps_log else f"无需处理: {validated}"
        return validated, process_desc
    
    @classmethod
    def process_itemized_scores(cls, itemized_scores_list, 
                                min_score: float, max_score: float,
                                rounding_step: float = 0.5,
                                logger: Optional[Callable] = None) -> Tuple[list, float]:
        """
        处理分项得分列表，返回清洗后的分数列表和总分。
        
        Args:
            itemized_scores_list: 分项得分列表（可能包含字符串等）
            min_score: 单项最低分
            max_score: 单项最高分（用于单项校验，总分可能超出）
            rounding_step: 四舍五入步长
            logger: 可选的日志记录函数
            
        Returns:
            (清洗后的分数列表, 计算的总分)
            
        Raises:
            ValueError: 任何分项无法清洗时
        """
        cleaned_scores = []
        for idx, score in enumerate(itemized_scores_list):
            try:
                cleaned = cls.sanitize(score)
                cleaned_scores.append(cleaned)
            except ValueError as e:
                raise ValueError(f"分项得分[{idx}] 清洗失败: {e}")
        
        total = sum(cleaned_scores)
        return cleaned_scores, total


# ==================== 向后兼容的辅助函数 ====================

def sanitize_score(val):
    """向后兼容：调用 ScoreProcessor.sanitize()"""
    return ScoreProcessor.sanitize(val)


def round_to_step(value: float, step: float) -> float:
    """向后兼容：调用 ScoreProcessor.round_to_step()"""
    return ScoreProcessor.round_to_step(value, step)


# ==================== 统一重试机制 ====================

class ErrorRetryability(Enum):
    """错误的可重试性分级（优先级从高到低）"""
    DEFINITELY_RETRYABLE = 1    # 明确可重试：网络超时、429限流、服务暂时不可用
    POSSIBLY_RETRYABLE = 2      # 可能可重试：Token过期、偶发5xx错误
    NOT_WORTH_RETRYING = 3      # 不值得重试：JSON格式错误、业务逻辑错误
    MANUAL_INTERVENTION = 4     # 需要人工介入：权限问题、功能缺陷


def extract_error_type_and_classify(error: Exception) -> Tuple[str, ErrorRetryability]:
    """提取错误类型并分类其可重试性
    
    Returns:
        (错误类型名称, 可重试性级别)
    """
    s = str(error).lower()
    
    # 1. 明确可重试的错误
    if 'timeout' in s or '超时' in s or 'timed out' in s:
        return ('timeout', ErrorRetryability.DEFINITELY_RETRYABLE)
    
    if '429' in s or 'rate limit' in s or '限流' in s or 'too many requests' in s:
        return ('rate_limit', ErrorRetryability.DEFINITELY_RETRYABLE)
    
    if 'connection' in s or '连接' in s or 'network' in s or '网络' in s:
        return ('network', ErrorRetryability.DEFINITELY_RETRYABLE)
    
    if '503' in s or 'service unavailable' in s or '服务不可用' in s:
        return ('service_unavailable', ErrorRetryability.DEFINITELY_RETRYABLE)
    
    # 2. 可能可重试的错误
    if 'token' in s or 'access_token' in s:
        # Token问题可能是过期，可以尝试刷新
        return ('token', ErrorRetryability.POSSIBLY_RETRYABLE)
    
    if '500' in s or '502' in s or '504' in s or 'internal server error' in s:
        # 偶发的服务器错误可能恢复
        return ('server_error', ErrorRetryability.POSSIBLY_RETRYABLE)
    
    # 3. 不值得重试的错误
    if 'json' in s or '格式' in s or 'parse' in s or '解析' in s:
        return ('json_parse', ErrorRetryability.NOT_WORTH_RETRYING)
    
    if '400' in s or 'bad request' in s or '请求错误' in s:
        return ('bad_request', ErrorRetryability.NOT_WORTH_RETRYING)
    
    if '404' in s or 'not found' in s:
        return ('not_found', ErrorRetryability.NOT_WORTH_RETRYING)
    
    if 'invalid' in s or '无效' in s or '非法' in s:
        return ('invalid_input', ErrorRetryability.NOT_WORTH_RETRYING)
    
    # 4. 需要人工介入的错误
    if '401' in s or '403' in s or 'unauthorized' in s or 'forbidden' in s or '权限' in s or '认证失败' in s:
        # 权限问题通常需要修改配置
        return ('permission', ErrorRetryability.MANUAL_INTERVENTION)
    
    if 'not implemented' in s or '未实现' in s or 'unsupported' in s:
        return ('not_implemented', ErrorRetryability.MANUAL_INTERVENTION)
    
    # 默认：未知错误，可能可重试
    return ('unknown', ErrorRetryability.POSSIBLY_RETRYABLE)


def calculate_smart_retry_delay(attempt: int, error_type: str, base_delay: float = 1.0) -> float:
    """根据错误类型和重试次数智能计算延迟时间（指数退避+错误感知）
    
    Args:
        attempt: 第几次重试（从1开始）
        error_type: 错误类型名称
        base_delay: 基础延迟时间（秒）
    
    Returns:
        延迟时间（秒）
    """
    # 不同错误类型的基础延迟倍数
    error_base_multipliers = {
        'rate_limit': 3.0,          # 限流：延迟长一些
        'timeout': 1.5,             # 超时：中等延迟
        'network': 1.0,             # 网络：正常延迟
        'token': 2.0,               # Token：稍长延迟（给时间刷新）
        'server_error': 2.0,        # 服务器错误：稍长延迟
        'service_unavailable': 2.5, # 服务不可用：较长延迟
    }
    
    multiplier = error_base_multipliers.get(error_type, 1.0)
    
    # 指数退避：第1次重试 = 基础延迟，第2次 = 2倍，第3次 = 4倍...
    exponential_factor = 2 ** (attempt - 1)
    
    # 添加随机抖动（±20%），避免多个请求同时重试
    jitter = random.uniform(0.8, 1.2)
    
    delay = base_delay * multiplier * exponential_factor * jitter
    
    # 设置最大延迟上限（避免等待太久）
    max_delay = 10.0
    return min(delay, max_delay)
def unified_retry(
    max_retries: int = 1,
    transient_error_checker: Optional[Callable[[Exception], bool]] = None,
    retry_delay: float = 1.0,
    log_callback: Optional[Callable[[str, bool, str], None]] = None,
    operation_name: str = "操作"
):
    """
    统一重试装饰器（v2.0），适用于OCR和API调用等需要重试的操作。
    
    新特性（v2.0）：
    - ✨ 指数退避：根据重试次数自动增加延迟（1s, 2s, 4s...）
    - ✨ 错误感知延迟：不同错误类型使用不同的基础延迟
    - ✨ 精细错误分类：四级分类（明确可重试、可能可重试、不值得重试、需人工介入）
    - ✨ 随机抖动：避免多个请求同时重试造成雪崩
    
    设计原则：
    - 统一重试次数为最多1次（首次+1次重试=共2次尝试）
    - 只对有重试价值的短暂性错误重试（网络、超时、token等）
    - 对业务/配置错误立即失败，不浪费调用次数
    - 节省token和OCR调用次数，降低开销
    
    Args:
        max_retries: 最大重试次数，默认1次（总共尝试2次）
        transient_error_checker: 函数，判断异常是否为短暂性可重试错误，接收Exception返回bool
        retry_delay: 重试前的基础延迟时间（秒），默认1.0秒（会根据错误类型智能调整）
        log_callback: 日志回调函数，签名为 (message: str, is_important: bool, level: str)
        operation_name: 操作名称，用于日志
    
    Returns:
        装饰器函数
    
    使用示例:
        @unified_retry(max_retries=1, transient_error_checker=self._is_transient_error,
                      log_callback=self.log_signal.emit, operation_name="OCR识别")
        def my_ocr_call():
            return self.api_service.call_baidu_ocr(...)
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            last_error_type = 'unknown'
            last_retryability = ErrorRetryability.POSSIBLY_RETRYABLE
            
            for attempt in range(max_retries + 1):  # +1 因为包含首次尝试
                try:
                    if attempt > 0:
                        # 计算智能延迟（指数退避+错误感知）
                        smart_delay = calculate_smart_retry_delay(
                            attempt=attempt,
                            error_type=last_error_type,
                            base_delay=retry_delay
                        )
                        
                        if log_callback:
                            # 显示更详细的重试信息
                            log_callback(
                                f"{operation_name}第{attempt}次重试（错误类型:{last_error_type}, 延迟{smart_delay:.1f}秒）...",
                                False, "DETAIL"
                            )
                        
                        time.sleep(smart_delay)
                    
                    # 执行实际操作
                    return func(*args, **kwargs)
                    
                except Exception as e:
                    last_exception = e
                    
                    # 提取错误类型并分类
                    error_type, retryability = extract_error_type_and_classify(e)
                    last_error_type = error_type
                    last_retryability = retryability
                    
                    # 判断是否应该重试（使用精细分类）
                    should_retry = False
                    
                    if retryability == ErrorRetryability.DEFINITELY_RETRYABLE:
                        # 明确可重试
                        should_retry = True
                    elif retryability == ErrorRetryability.POSSIBLY_RETRYABLE:
                        # 可能可重试，使用旧的检查器兼容
                        if transient_error_checker:
                            try:
                                should_retry = transient_error_checker(e)
                            except:
                                should_retry = True  # 默认重试一次
                        else:
                            should_retry = True
                    elif retryability == ErrorRetryability.NOT_WORTH_RETRYING:
                        # 不值得重试（如JSON格式错误）
                        should_retry = False
                        if log_callback:
                            log_callback(
                                f"{operation_name}失败（{error_type}错误不值得重试）: {str(e)}",
                                True, "ERROR"
                            )
                    else:  # MANUAL_INTERVENTION
                        # 需要人工介入（如权限问题）
                        should_retry = False
                        if log_callback:
                            log_callback(
                                f"{operation_name}失败（{error_type}错误需要人工介入）: {str(e)}",
                                True, "ERROR"
                            )
                    
                    # 根据判断决定是否重试
                    if not should_retry:
                        raise
                    
                    # 短暂性错误的处理
                    if attempt < max_retries:
                        # 还有重试机会
                        if log_callback:
                            log_callback(
                                f"{operation_name}尝试{attempt+1}/{max_retries+1}失败（{error_type}错误）: {str(e)[:100]}，将智能重试",
                                True, "WARNING"
                            )
                    else:
                        # 最后一次尝试也失败了
                        if log_callback:
                            log_callback(
                                f"{operation_name}失败（已重试{max_retries}次，{error_type}错误）: {str(e)}",
                                True, "ERROR"
                            )
                        raise
            
            # 理论上不会到这里，但为了安全
            if last_exception:
                raise last_exception
            
        return wrapper
    return decorator


# 题型差异化OCR阈值配置表（v3.0 - 2025-12-12三档系统）
OCR_QUALITY_THRESHOLDS = {
    'relaxed': {
        'Objective_FillInTheBlank': {'avg': 0.80, 'min': 0.75, 'low_ratio': 0.22, 'reason': '填空题-宽松模式（通过率约85%）'},
        'Subjective_PointBased_QA': {'avg': 0.67, 'min': 0.52, 'low_ratio': 0.40, 'reason': '主观题-宽松模式'},
        'Formula_Proof_StepBased': {'avg': 0.62, 'min': 0.47, 'low_ratio': 0.45, 'reason': '公式题-宽松模式'},
        'Holistic_Evaluation_Open': {'avg': 0.57, 'min': 0.42, 'low_ratio': 0.50, 'reason': '作文-宽松模式'}
    },
    'moderate': {
        'Objective_FillInTheBlank': {'avg': 0.88, 'min': 0.83, 'low_ratio': 0.12, 'reason': '填空题-适度模式（推荐，通过率约75%）'},
        'Subjective_PointBased_QA': {'avg': 0.75, 'min': 0.60, 'low_ratio': 0.30, 'reason': '主观题-适度模式'},
        'Formula_Proof_StepBased': {'avg': 0.70, 'min': 0.55, 'low_ratio': 0.35, 'reason': '公式题-适度模式'},
        'Holistic_Evaluation_Open': {'avg': 0.65, 'min': 0.50, 'low_ratio': 0.40, 'reason': '作文-适度模式'}
    },
    'strict': {
        'Objective_FillInTheBlank': {'avg': 0.96, 'min': 0.91, 'low_ratio': 0.02, 'reason': '填空题-严格模式（通过率约60%）'},
        'Subjective_PointBased_QA': {'avg': 0.83, 'min': 0.68, 'low_ratio': 0.20, 'reason': '主观题-严格模式'},
        'Formula_Proof_StepBased': {'avg': 0.78, 'min': 0.63, 'low_ratio': 0.25, 'reason': '公式题-严格模式'},
        'Holistic_Evaluation_Open': {'avg': 0.73, 'min': 0.58, 'low_ratio': 0.30, 'reason': '作文-严格模式'}
    }
}

# 函数：将数值四舍五入到指定步长的倍数（支持0.5或1.0，默认0.5）

# 原有的 sanitize_score、round_to_step 函数已迁移到 ScoreProcessor 类
# 保留了向后兼容的包装函数在类定义之后


class GradingThread(QThread):
    # 信号定义
    log_signal = pyqtSignal(str, bool, str)
    progress_signal = pyqtSignal(int, int)
    finished_signal = pyqtSignal()
    error_signal = pyqtSignal(str)
    threshold_exceeded_signal = pyqtSignal(str)
    manual_intervention_signal = pyqtSignal(str, str)
    record_signal = pyqtSignal(dict)

    def __init__(self, api_service, config_manager=None):
        super().__init__()
        self.api_service = api_service
        self.config_manager = config_manager
        self.parameters = {}
        self.running = False
        self.completion_status = "idle"  # idle, running, completed, error, threshold_exceeded
        self.interrupt_reason = ""
        self.completed_count = 0
        self.total_question_count_in_run = 0
        self.max_score = 100
        self.min_score = 0
        self.first_model_id = ''
        self.second_model_id = ''
        self.is_single_question_one_run = False
        
        # =================================================================
        # P0修复：线程安全与并发问题
        # =================================================================
        # 添加线程锁保护共享资源的并发访问
        self._params_lock = Lock()  # 保护self.parameters
        self._state_lock = Lock()   # 保护completion_status等状态变量
        self._temp_resources = []   # 追踪临时资源（图片对象等）以便清理

    def _get_common_system_message(self, ocr_mode: bool = False, include_evidence_bar: bool = True) -> str:
        """
        返回通用的AI系统提示词。
        
        Args:
            ocr_mode: 是否为OCR模式（OCR模式不需要处理图片涂改等信息）
            include_evidence_bar: 是否在返回的系统提示中包含证据门槛段（默认包含）
        
        Returns:
            str: 系统提示词
        """
        subject = "通用"

        # 尝试从 config_manager 获取科目
        subject_from_config = None
        if self.config_manager:
            try:
                subject_from_config = getattr(self.config_manager, 'subject', None)
            except Exception:
                pass

        if subject_from_config and isinstance(subject_from_config, str) and subject_from_config.strip():
            subject = subject_from_config.strip()

        # 统一的人工介入协议（适用于 OCR 与 非OCR 模式）
        intervention_protocol = (
            "【人工介入协议】\n"
            "- 宁停勿错。当你认为无法合理判分，需要人工介入时： scoring_basis 必须以 \"需人工介入: \" 开头；itemized_scores 全0（长度尽量与采分点一致，不确定则 [0]）。\n"
            "- 以下情况必须触发人工介入：\n"
            "- 学生答案无法有效识别（如OCR识别文本出现明显乱码）\n"
            "- 关键采分点的判定依据模糊（如公式书写不清）\n"
        )

        # JSON 输出规范：对 OCR 模式和非 OCR 模式分别给出不同的要求
        json_compliance_nonocr = (
            "【JSON输出】\n"
            "- 只输出一个JSON对象（不要代码块、不要解释）。必须包含键：student_answer_summary, scoring_basis, itemized_scores。\n"
            "- JSON键用双引号\n"
            "- itemized_scores 只能是纯数字数组，数组长度与评分细则的采分点数量严格一致。示例: [2, 0.5, 0]\n"
        )

        json_compliance_ocr = (
            "【JSON输出】\n"
            "- 只输出一个JSON对象（不要代码块、不要解释）。必须包含键：scoring_basis, itemized_scores。\n"
            "- JSON键用双引号\n"
            "- itemized_scores 只能是纯数字数组，数组长度与评分细则的采分点数量严格一致。示例: [2, 0.5, 0]\n"
        )

        evidence_bar = (
            "【证据门槛】\n"
            "- 只有在学生答案中能找到可定位的直接证据时才给分；无法评分则触发人工介入协议（不要想象/猜/补全）。\n"
            "- scoring_basis 逐点/逐空/逐步给出： 判定 + 得X分 + 简要证据。证据用【…】包裹，不要使用英文双引号字符 \"\"（避免JSON解析失败）。\n"
            "  示例：第1点 未命中 得0分 证据:【...】\n"
            "- 若学生答案空白/仅有涂改痕迹/无有效内容/乱写/答非所问/全错，可依据评分细则给0分，在scoring_basis说明判定理由和证据；判定0分必须有证据（禁止想象/猜/补全），无法判断就人工介入。\n"
        )


        penalty_rules = (
            "【扣分条款】\n"
            "- 若细则有扣分条款：先按采分点给分，再按条款扣分；扣分也必须有证据，无法判断就人工介入。\n"
        )

        # 组装系统消息
        if ocr_mode:
            base_msg = (
                f"你是一位经验丰富、严谨细致的【{subject}】资深阅卷老师。\n"
                "当前为OCR纯学生答案文本模式：只能严格依据OCR文本和评分细则评分，禁止用常识猜想/补全内容。如发现疑似OCR误识导致无法判分的情况，触发人工介入协议\n"
                + intervention_protocol
            )
        else:
            base_msg = (
                f"你是一位经验丰富、严谨细致的【{subject}】资深阅卷老师。\n"
                "必须严格依据学生答案图片内容和评分细则评分；对划掉/删除线明确作废的内容不计分。\n\n"
                + intervention_protocol
            )

        if include_evidence_bar:
            base_msg += evidence_bar

        base_msg += penalty_rules + (json_compliance_ocr if ocr_mode else json_compliance_nonocr)
        return base_msg


    def _build_objective_fillintheblank_prompt(self, standard_answer_rubric: str, ocr_mode: bool = False):
        system_message = self._get_common_system_message(ocr_mode=ocr_mode)
        user_prompt = (
            "【题目类型：客观填空题】\n"
            "- 逐空对照评分细则判定得分；若细则允许同义/近义给分，请在 scoring_basis 给出【证据】。\n\n"
            "【评分细则】\n"
            f"{standard_answer_rubric.strip()}\n"
        )
        return {"system": system_message, "user": user_prompt}


    def _build_subjective_pointbased_prompt(self, standard_answer_rubric: str, ocr_mode: bool = False):
        system_message = self._get_common_system_message(ocr_mode=ocr_mode)
        user_prompt = (
            "【题目类型：按点给分主观题】\n"
            "- 逐点对照评分细则判定并给分；每点在 scoring_basis 给出【证据】，禁止凭印象补全。\n\n"
            "【评分细则】\n"
            f"{standard_answer_rubric.strip()}\n"
        )
        return {"system": system_message, "user": user_prompt}


    def _build_formula_proof_prompt(self, standard_answer_rubric: str, ocr_mode: bool = False):
        system_message = self._get_common_system_message(ocr_mode=ocr_mode)
        user_prompt = (
            "【题目类型：公式计算/证明题】\n"
            "- 按评分细则的步骤/采分点核对：公式、代入、计算/推理、符号等；证据不足、无法评分就触发人工介入协议。\n\n"
            "【评分细则】\n"
            f"{standard_answer_rubric.strip()}\n"
        )
        return {"system": system_message, "user": user_prompt}


    def _build_holistic_evaluation_prompt(self, standard_answer_rubric: str, ocr_mode: bool = False):
        system_message = self._get_common_system_message(ocr_mode=ocr_mode, include_evidence_bar=False)
        user_prompt = (
            "【题目类型：整体评估开放题】\n"
            "- 仅依据评分细则和学生答案给出总分；在 scoring_basis 说明评分理由。\n\n"
            "【评分细则】\n"
            f"{standard_answer_rubric.strip()}\n"
        )
        return {"system": system_message, "user": user_prompt}

    def select_and_build_prompt(self, standard_answer, question_type, ocr_mode: bool = False):
        """根据题目类型选择并构建相应的Prompt。

        返回结构：
            {"system": <system_message_str>, "user": <user_prompt_str>}

        说明：
        - system 会作为真正的 system role 发送（由 api_service 负责）
        - user 是评分任务指令文本（非JSON载体），模型输出仍必须为JSON
        """
        # 确保 standard_answer 是字符串类型，如果不是，尝试转换或记录错误
        if not isinstance(standard_answer, str):
            self.log_signal.emit(f"评分细则不是字符串类型 (实际类型: {type(standard_answer)})，尝试转换。", True, "ERROR")
            try:
                standard_answer = str(standard_answer) # 尝试转换
            except Exception as e:
                error_msg = f"评分细则无法转换为字符串 (错误: {e})，阅卷已暂停，请检查配置并手动处理当前题目。"
                self.log_signal.emit(error_msg, True, "ERROR")
                self._set_error_state(error_msg)
                return None # 中断处理

        # 再次检查 standard_answer 是否有效 (可能转换后仍为空或在初始就是空)
        if not standard_answer or not standard_answer.strip():
            error_msg = "评分细则为空，阅卷已暂停，请输入评分细则或手动处理当前题目。"
            self.log_signal.emit(error_msg, True, "ERROR")
            self._set_error_state(error_msg)
            return None # 中断处理


        if question_type == "Objective_FillInTheBlank": # 更新了类型名称
            return self._build_objective_fillintheblank_prompt(standard_answer, ocr_mode=ocr_mode)
        elif question_type == "Subjective_PointBased_QA":
            return self._build_subjective_pointbased_prompt(standard_answer, ocr_mode=ocr_mode)
        elif question_type == "Formula_Proof_StepBased":
            return self._build_formula_proof_prompt(standard_answer, ocr_mode=ocr_mode)
        elif question_type == "Holistic_Evaluation_Open":
            return self._build_holistic_evaluation_prompt(standard_answer, ocr_mode=ocr_mode)
        else:
            self.log_signal.emit(f"未知的题目类型: '{question_type}'，将使用默认的按点给分主观题Prompt。", True, "WARNING")
            return self._build_subjective_pointbased_prompt(standard_answer, ocr_mode=ocr_mode)
    # --- 结束新增的Prompt构建方法 ---

    def _is_unrecognizable_answer(self, student_answer_summary, itemized_scores):
        """
        检查学生答案是否无法识别。
        当AI判断图片无法识别时，会返回特定的摘要内容和全零分。

        Args:
            student_answer_summary: AI返回的学生答案摘要
            itemized_scores: 分项得分列表

        Returns:
            bool: True表示无法识别，False表示可以识别
        """
        if not student_answer_summary:
            return False

        # 检查摘要是否包含无法识别的关键词
        unrecognizable_keywords = [
            "无法", "无法识别", "字迹模糊", "无法辨认", "完全空白",
            "图片内容完全无法识别", "字迹完全无法辨认", "未作答",
            "学生未作答", "答题区域空白", "无任何作答痕迹"
        ]

        summary_lower = student_answer_summary.lower()
        for keyword in unrecognizable_keywords:
            if keyword in summary_lower:
                return True

        # 检查是否全零分且摘要表明无法识别
        if itemized_scores and isinstance(itemized_scores, list):
            # 检查是否全为0
            all_zero = all(score == 0 for score in itemized_scores)
            if all_zero and any(keyword in summary_lower for keyword in unrecognizable_keywords):
                return True

        return False

    def _is_ai_requesting_image_content(self, student_answer_summary, scoring_basis):
        """
        检查AI是否在请求提供学生答案图片内容。
        当AI无法从图片中提取有效信息时，会返回特定的提示内容。

        Args:
            student_answer_summary: AI返回的学生答案摘要
            scoring_basis: AI返回的评分依据

        Returns:
            bool: True表示AI在请求图片内容，False表示正常响应
        """
        # 如果摘要与评分依据都为空，则无法判断
        if not student_answer_summary and not scoring_basis:
            return False

        summary_lower = (student_answer_summary or "").lower()
        basis_lower = (scoring_basis or "").lower()

        # 检查是否包含请求图片内容的关键词
        request_keywords = [
            "请提供图片", "请提供原图", "看不清", "看不清楚", "请上传图片", "需要原图", "请给出图片",
            "图片无法识别", "图片不清晰", "请提供照片", "请提供答题图片"
        ]

        for keyword in request_keywords:
            if keyword in summary_lower or keyword in basis_lower:
                return True

        return False

    def _cleanup_resources(self):
        """清理临时资源（图片对象、BytesIO等）
        
        P0修复：确保释放所有临时资源，防止内存泄漏
        """
        try:
            # 清理追踪的临时资源
            for resource in self._temp_resources:
                try:
                    if hasattr(resource, 'close'):
                        resource.close()
                except:
                    pass
            self._temp_resources.clear()
        except Exception as e:
            self.log_signal.emit(f"清理临时资源时出错: {str(e)}", False, "WARNING")

    def _is_transient_error(self, error_msg) -> bool:
        """判断错误信息是否为短暂/可重试的网络/超时/token类错误（v2.0增强版）。

        使用新的精细错误分类系统，只有明确可重试和可能可重试的错误才返回True。
        """
        if not error_msg:
            return False

        # 将错误消息转换为异常对象以使用新的分类系统
        try:
            # 使用新的精细分类系统
            _, retryability = extract_error_type_and_classify(RuntimeError(str(error_msg)))
            
            # 只有明确可重试和可能可重试的错误才返回True
            return retryability in (ErrorRetryability.DEFINITELY_RETRYABLE, 
                                   ErrorRetryability.POSSIBLY_RETRYABLE)
        except:
            # 如果分类失败，使用保守策略：假定可重试
            return True

    def _set_error_state(self, reason, error: Optional[GradingError] = None):
        """统一设置错误状态（线程安全）
        
        Args:
            reason: 错误原因描述（字符串或GradingError实例）
            error: 可选的GradingError实例，用于获取更精确的恢复策略
        """
        # 如果reason是GradingError实例，提取信息
        if isinstance(reason, GradingError):
            error = reason
            reason = ErrorRecoveryManager.format_error_message(error)
        
        # 获取恢复策略
        if error:
            strategy = ErrorRecoveryManager.get_recovery_strategy(error)
            log_level = strategy.get('log_level', 'ERROR')
        else:
            log_level = 'ERROR'
        
        with self._state_lock:
            self.completion_status = "error"
            self.interrupt_reason = str(reason)
            self.running = False
        
        self.log_signal.emit(f"错误: {reason}", True, log_level)

    def _process_single_question(self, q_config: dict, q_idx: int, num_questions: int,
                                  dual_evaluation: bool, score_diff_threshold: float) -> bool:
        """处理单个题目的阅卷流程
        
        将题目处理逻辑从run()方法中提取出来，降低复杂度。
        
        Args:
            q_config: 题目配置字典
            q_idx: 题目在列表中的索引（0-based）
            num_questions: 总题目数
            dual_evaluation: 是否启用双评
            score_diff_threshold: 双评分差阈值
            
        Returns:
            bool: True表示处理成功并可继续，False表示需要停止
        """
        question_index = q_config.get('question_index', q_idx + 1)
        self.log_signal.emit(f"正在处理第 {question_index} 题（本轮第 {q_idx + 1}/{num_questions} 题）", False, "DETAIL")

        # 设置当前题目索引
        self.api_service.set_current_question(question_index)

        # 获取题目配置
        score_input_pos = q_config.get('score_input_pos', (0, 0))
        confirm_button_pos = q_config.get('confirm_button_pos', (0, 0))
        standard_answer = q_config.get('standard_answer', '')
        score_rounding_step = q_config.get('score_rounding_step', 0.5)
        q_min_score = float(q_config.get('min_score', self.min_score))
        q_max_score = float(q_config.get('max_score', self.max_score))

        # 检查位置配置
        if score_input_pos == (0, 0) or confirm_button_pos == (0, 0):
            self._set_error_state(
                ConfigError(f"第 {question_index} 题未配置位置信息",
                           config_key=f"question_{question_index}_position")
            )
            return False

        # 获取并验证答案区域
        answer_area_data = q_config.get('answer_area', {})
        if not answer_area_data or not all(key in answer_area_data for key in ['x1', 'y1', 'x2', 'y2']):
            self._set_error_state(
                ConfigError(f"第 {question_index} 题未配置答案区域",
                           config_key=f"question_{question_index}_answer_area")
            )
            return False

        # 获取题目类型
        question_type = q_config.get('question_type', 'Subjective_PointBased_QA')
        if not question_type:
            self.log_signal.emit(f"警告：第 {question_index} 题未配置题目类型，使用默认类型", True, "WARNING")
            question_type = 'Subjective_PointBased_QA'

        # 截取答案区域
        img_str = self._capture_question_area(answer_area_data)
        if img_str is None or not self.running:
            return False

        # 处理OCR识别（如果启用）
        ocr_result = self._handle_ocr_recognition(q_config, question_index, img_str, question_type)
        if ocr_result is None:
            return False
        ocr_text, ocr_meta, is_baidu_ocr_mode = ocr_result

        # 构建Prompt
        text_prompt_for_api = self.select_and_build_prompt(standard_answer, question_type, ocr_mode=is_baidu_ocr_mode)
        if text_prompt_for_api is None:
            return self.running  # 如果running为False则停止，否则继续下一题

        # 调用API评分
        img_for_api = "" if is_baidu_ocr_mode else img_str
        eval_result = self.evaluate_answer(
            img_for_api, text_prompt_for_api, q_config, dual_evaluation, score_diff_threshold, ocr_text
        )

        # 处理评分结果
        if eval_result is None:
            self.log_signal.emit(f"题目{question_index} 评分处理完全失败", True, "ERROR")
            self._set_error_state(
                BusinessError(f"题目{question_index} 评分处理失败，需手动处理",
                             BusinessError.TYPE_API_RESPONSE, question_index=question_index)
            )
            return False

        score, reasoning_data, itemized_scores_data, confidence_data, raw_ai_response = eval_result

        if score is None:
            self.log_signal.emit(f"第 {question_index} 题评分失败", True, "ERROR")
            self._set_error_state(
                BusinessError(f"第 {question_index} 题评分失败，需手动处理",
                             BusinessError.TYPE_SCORE_PARSE, question_index=question_index)
            )
            return False

        # 处理分数
        try:
            processed_score, process_log = ScoreProcessor.process_pipeline(
                score, q_min_score, q_max_score, score_rounding_step, logger=self.log_signal.emit
            )
            score = processed_score
            self.log_signal.emit(f"题目{question_index} 分数处理: {process_log}", False, "DETAIL")
        except Exception as e:
            self.log_signal.emit(f"题目{question_index} 分数处理失败: {e}", True, "ERROR")
            self._set_error_state(
                BusinessError(f"题目{question_index} 分数处理失败：{e}",
                             BusinessError.TYPE_SCORE_PARSE, question_index=question_index, original_error=e)
            )
            return False

        # 输入分数
        self.input_score(score, score_input_pos, confirm_button_pos, q_config)
        if not self.running:
            return False

        # 记录阅卷结果
        self.record_grading_result(question_index, score, img_str, reasoning_data,
                                   itemized_scores_data, confidence_data, raw_ai_response, ocr_text, ocr_meta)

        # 题目间等待
        if q_idx < num_questions - 1 and self.running:
            time.sleep(0.5)

        return True

    def _capture_question_area(self, answer_area_data: dict) -> Optional[str]:
        """截取答案区域图像
        
        Args:
            answer_area_data: 包含x1,y1,x2,y2的区域字典
            
        Returns:
            base64编码的图片字符串，失败返回None
        """
        x1 = answer_area_data.get('x1', 0)
        y1 = answer_area_data.get('y1', 0)
        x2 = answer_area_data.get('x2', 0)
        y2 = answer_area_data.get('y2', 0)

        x = min(x1, x2)
        y = min(y1, y2)
        width = abs(x2 - x1)
        height = abs(y2 - y1)

        return self.capture_answer_area((x, y, width, height))

    def _handle_ocr_recognition(self, q_config: dict, question_index: int, 
                                 img_str: str, question_type: str) -> Optional[tuple]:
        """处理OCR识别逻辑
        
        Args:
            q_config: 题目配置
            question_index: 题目索引
            img_str: 图片base64字符串
            question_type: 题目类型
            
        Returns:
            (ocr_text, ocr_meta, is_baidu_ocr_mode) 元组，失败返回None
        """
        q_ocr_mode_index = q_config.get('ocr_mode_index', 0)
        is_baidu_ocr_mode = (q_ocr_mode_index == 1)
        q_ocr_quality_level = q_config.get('ocr_quality_level', 'moderate')
        
        self.log_signal.emit(
            f"题目{question_index} OCR模式: {'百度OCR' if is_baidu_ocr_mode else '纯AI'}, 精度: {q_ocr_quality_level}",
            False, "DETAIL"
        )

        ocr_text = ""
        ocr_meta = None

        if not is_baidu_ocr_mode:
            return (ocr_text, ocr_meta, is_baidu_ocr_mode)

        # 执行OCR识别
        ocr_text, ocr_meta = self._perform_ocr_recognition(img_str, question_type, ocr_quality_level=q_ocr_quality_level)
        
        # 显示OCR结果
        if ocr_text and isinstance(ocr_text, str) and ocr_text.strip():
            self.log_signal.emit(f"题目{question_index} OCR识别结果: {ocr_text[:200]}", False, "RESULT")
        else:
            self.log_signal.emit(f"题目{question_index} OCR未能识别到文字", False, "RESULT")

        # 检查是否需要人工介入
        if ocr_meta and ocr_meta.get('manual_intervention'):
            reason = ocr_meta.get('reason') or 'OCR质量不达标，需人工介入'
            self.log_signal.emit(f"题目{question_index} OCR质量不足，暂停阅卷: {reason}", True, "ERROR")
            try:
                self.manual_intervention_signal.emit(reason, ocr_text)
            except Exception:
                pass
            self._set_error_state(
                BusinessError(f"题目{question_index} OCR质量不足，人工复核: {reason}",
                             BusinessError.TYPE_OCR_FAILURE, question_index=question_index)
            )
            return None

        # 检查OCR结果有效性
        if (ocr_meta is None) or (not ocr_text or not ocr_text.strip()):
            reason = f'题目{question_index} OCR未能识别到有效文本或未返回OCR元信息，需人工介入'
            self.log_signal.emit(f"OCR识别文本为空或元信息缺失: {reason}", True, "ERROR")
            self._set_error_state(
                BusinessError(reason, BusinessError.TYPE_OCR_FAILURE, question_index=question_index)
            )
            return None

        return (ocr_text, ocr_meta, is_baidu_ocr_mode)

    def _handle_grading_exception(self, e: Exception) -> None:
        """统一处理阅卷过程中的异常
        
        Args:
            e: 捕获的异常
        """
        error_detail = traceback.format_exc()
        
        # 根据异常类型进行分类处理
        if isinstance(e, (ConfigError, NetworkError, BusinessError, ResourceError)):
            classified_error = e
        elif isinstance(e, ValueError):
            classified_error = ErrorRecoveryManager.classify_exception(e)
        elif isinstance(e, KeyError):
            classified_error = ConfigError(f"配置字段缺失: {str(e)}", config_key=str(e), original_error=e)
        elif isinstance(e, (IOError, OSError, FileNotFoundError, PermissionError)):
            classified_error = ResourceError(str(e), ResourceError.TYPE_FILE_IO, original_error=e)
        else:
            classified_error = ErrorRecoveryManager.classify_exception(e)
        
        strategy = ErrorRecoveryManager.get_recovery_strategy(classified_error)
        formatted_msg = ErrorRecoveryManager.format_error_message(classified_error)
        
        self.log_signal.emit(f"{formatted_msg}\n{error_detail}", True, strategy['log_level'])
        
        # 线程安全地设置完成状态与中断原因，确保与 _set_error_state 的行为一致
        with self._state_lock:
            if isinstance(classified_error, BusinessError) and classified_error.error_type == BusinessError.TYPE_DUAL_EVAL:
                self.completion_status = "threshold_exceeded"
            else:
                self.completion_status = "error"

            self.interrupt_reason = formatted_msg
            self.running = False
        
        # 网络错误提供重试建议
        if isinstance(classified_error, NetworkError) and strategy['should_retry']:
            self.log_signal.emit(
                f"网络错误可重试，建议等待 {strategy['retry_delay']:.1f} 秒后重新开始",
                False, "INFO"
            )

    def _finalize_run(self, cycle_number: int, dual_evaluation: bool, 
                      score_diff_threshold: float, elapsed_time: float) -> None:
        """run()方法的收尾工作：清理资源、生成汇总、发送信号
        
        Args:
            cycle_number: 循环次数
            dual_evaluation: 是否双评
            score_diff_threshold: 分差阈值
            elapsed_time: 运行时间
        """
        self.running = False
        
        # 清理临时资源
        try:
            self._cleanup_resources()
        except Exception as cleanup_error:
            try:
                self.log_signal.emit(f"资源清理失败: {str(cleanup_error)}", False, "WARNING")
            except:
                pass
        
        # 生成汇总记录
        try:
            self.generate_summary_record(cycle_number, dual_evaluation, score_diff_threshold, elapsed_time)
        except Exception as summary_error:
            try:
                self.log_signal.emit(f"生成汇总记录失败: {str(summary_error)}", True, "ERROR")
            except Exception:
                print(f"[严重错误] 生成汇总记录失败且无法发送日志: {summary_error}")

        # 发送完成信号
        self._emit_completion_signal()

    def _emit_completion_signal(self) -> None:
        """根据完成状态发送相应的信号"""
        reason = self.interrupt_reason or "未知错误"
        
        try:
            if self.completion_status == "completed":
                try:
                    self.finished_signal.emit()
                except Exception as e:
                    print(f"[严重错误] 发送finished_signal失败: {e}")
            elif self.completion_status == "threshold_exceeded":
                try:
                    self.threshold_exceeded_signal.emit(reason if reason != "未知错误" else "双评分差超过阈值")
                except Exception as e:
                    print(f"[严重错误] 发送threshold_exceeded_signal失败: {e}")
                    try:
                        self.error_signal.emit(reason)
                    except Exception:
                        pass
            else:
                try:
                    self.error_signal.emit(reason)
                except Exception as e:
                    print(f"[严重错误] 发送error_signal失败: {e}")
        except Exception as final_error:
            print(f"[致命错误] 发送信号时出现异常: {final_error}")
            try:
                self.log_signal.emit(
                    f"阅卷线程终止时发生致命错误，状态={self.completion_status}: {final_error}",
                    True, "ERROR"
                )
            except Exception:
                pass

    def run(self):
        """线程主函数，执行自动阅卷流程
        
        重构说明：将复杂的题目处理逻辑提取到 _process_single_question() 等辅助方法中，
        显著降低本方法的圈复杂度，使其更易于维护和测试。
        """
        # 重置状态
        self.completion_status = "running"
        self.completed_count = 0
        self.total_question_count_in_run = 0
        self.interrupt_reason = ""
        self.running = True
        self.log_signal.emit("自动阅卷线程已启动", False, "INFO")

        # 为finally块提供安全的默认值
        cycle_number = 0
        wait_time = 0
        question_configs = []
        dual_evaluation = False
        score_diff_threshold = 10
        start_time = time.time()
        elapsed_time = 0

        try:
            # 获取参数（线程安全）
            with self._params_lock:
                params = self.parameters.copy()
            
            cycle_number = int(params.get('cycle_number', 1)) if isinstance(params, dict) else 1
            wait_time = params.get('wait_time', 1) if isinstance(params, dict) else 1
            question_configs = params.get('question_configs', []) if isinstance(params, dict) else []
            dual_evaluation = params.get('dual_evaluation', False) if isinstance(params, dict) else False
            score_diff_threshold = params.get('score_diff_threshold', 10) if isinstance(params, dict) else 10
            self.log_signal.emit("OCR模式已变更为各小题独立配置", False, "DETAIL")

            if not question_configs:
                self._set_error_state(ConfigError("未配置题目信息", config_key="question_configs"))
                return

            num_questions = len(question_configs)
            self.total_question_count_in_run = num_questions
            self.log_signal.emit(f"多题模式：本次阅卷共 {num_questions} 道题目", False, "INFO")

            start_time = time.time()

            # 主循环：执行多轮阅卷
            for i in range(cycle_number):
                if not self.running:
                    break

                self.log_signal.emit(f"开始第 {i+1}/{cycle_number} 次阅卷（共 {num_questions} 题）", False, "DETAIL")

                # 题目循环：使用提取的辅助方法处理每个题目
                for q_idx, q_config in enumerate(question_configs):
                    if not self.running:
                        break
                    
                    success = self._process_single_question(
                        q_config, q_idx, num_questions, dual_evaluation, score_diff_threshold
                    )
                    if not success:
                        break

                if not self.running:
                    break

                # 更新进度
                self.completed_count = i + 1
                self.progress_signal.emit(self.completed_count, cycle_number)

                # 轮次间等待
                if self.running and wait_time > 0 and i < cycle_number - 1:
                    self.log_signal.emit(f"等待 {wait_time} 秒后开始下一轮...", False, "DETAIL")
                    time.sleep(wait_time)

            # 计算总用时
            elapsed_time = time.time() - start_time
            if self.running:
                self.log_signal.emit(f"自动阅卷完成，总用时: {elapsed_time:.2f} 秒", False, "INFO")
                self.completion_status = "completed"
            elif self.completion_status == "running":
                self.completion_status = "error"
                self.interrupt_reason = "未知错误导致中断"

        except (ConfigError, NetworkError, BusinessError, ResourceError) as e:
            self._handle_grading_exception(e)
        except ValueError as e:
            self._handle_grading_exception(e)
        except KeyError as e:
            self._handle_grading_exception(e)
        except (IOError, OSError, FileNotFoundError, PermissionError) as e:
            self._handle_grading_exception(e)
        except Exception as e:
            self._handle_grading_exception(e)

        finally:
            self._finalize_run(cycle_number, dual_evaluation, score_diff_threshold, elapsed_time)

    def set_parameters(self, **kwargs):
        """设置线程参数（线程安全）"""
        with self._params_lock:
            self.parameters = kwargs
            if 'max_score' in kwargs:
                self.max_score = kwargs['max_score']
            if 'min_score' in kwargs:
                self.min_score = kwargs['min_score']

        # 保存API配置信息
        self.first_model_id = kwargs.get('first_model_id', '')
        self.second_model_id = kwargs.get('second_model_id', '')
        self.is_single_question_one_run = kwargs.get('is_single_question_one_run', False)

    def stop(self):
        """停止线程（线程安全）"""
        with self._state_lock:
            self.running = False
        if self.completion_status == "running":
            self.completion_status = "error"
            self.interrupt_reason = "用户手动停止"
        self.log_signal.emit("正在停止自动阅卷线程...", False, "INFO")

    def capture_answer_area(self, area):
        """截取答案区域，带统一重试机制（最多重试1次）

        Args:
            area: 答案区域坐标 (x, y, width, height)

        Returns:
            base64编码的图片字符串，失败时直接停止整个流程
        """
        x, y, width, height = area

        # 确保宽度和高度为正值
        if width < 0:
            x = x + width
            width = abs(width)
        if height < 0:
            y = y + height
            height = abs(height)

        # 内部实现函数
        def _do_capture():
            screenshot = None
            try:
                self.log_signal.emit(f"正在截取答案区域 (坐标: {x},{y}, 尺寸: {width}x{height})", False, "DETAIL")

                # 截取屏幕指定区域
                # P0修复：使用try-finally确保PIL Image资源释放
                screenshot = ImageGrab.grab(bbox=(x, y, x + width, y + height))
                
                try:
                    # 转换为带Data URI前缀的base64字符串
                    buffered = BytesIO()
                    try:
                        screenshot.save(buffered, format="JPEG")
                        base64_data = base64.b64encode(buffered.getvalue()).decode()
                        img_str = f"data:image/jpeg;base64,{base64_data}"
                        self.log_signal.emit(f"答案区域截取成功 (图片大小: {len(base64_data)} 字节)", False, "INFO")
                        return img_str
                    finally:
                        # 确保BytesIO被关闭
                        buffered.close()
                finally:
                    # 确保PIL Image对象被释放
                    if screenshot:
                        screenshot.close()
                        screenshot = None

            except Exception as e:
                # 异常处理中也要确保清理资源
                if screenshot:
                    try:
                        screenshot.close()
                    except:
                        pass
                    screenshot = None
                raise  # 重新抛出异常供统一重试机制处理

        # 使用统一重试机制（截图失败通常是短暂性错误，如系统繁忙）
        try:
            @unified_retry(
                max_retries=1,
                transient_error_checker=lambda e: True,  # 截图错误一般都可重试
                log_callback=self.log_signal.emit,
                operation_name="截取答案区域"
            )
            def _capture_with_retry():
                return _do_capture()
            
            return _capture_with_retry()
        except Exception as e:
            # 所有重试都失败了，停止整个流程
            final_error = f"截取答案区域失败（已重试1次）。坐标: ({x},{y}), 尺寸: {width}x{height}。错误: {str(e)}"
            self._set_error_state(final_error)
            return None

    def _preprocess_image_for_ocr(self, img_str: str) -> str:
        """对传入的base64图片进行降采样和灰度处理以提升OCR稳定性。

        返回处理后的data:image/jpeg;base64,... 字符串。发生错误时返回原图字符串。
        配置项（可选，均在 `self.api_service.config_manager` 中）：
          - ocr_preprocess_to_gray (bool, default True)
          - ocr_preprocess_max_width (int, default 1200)
          - ocr_preprocess_jpeg_quality (int, default 85)
        """
        img = None
        buffered = None
        try:
            if not img_str or not isinstance(img_str, str) or not img_str.startswith('data:image'):
                return img_str

            header, b64data = img_str.split(',', 1)
            img_bytes = base64.b64decode(b64data)
            
            # P0修复：使用上下文管理器或明确的try-finally确保Image对象释放
            bytes_io = BytesIO(img_bytes)
            try:
                # 重要：Pillow 的 convert()/resize() 会返回新 Image 对象。
                # 若直接用 img = img.convert(...) 覆盖引用，原 Image 可能无法及时 close，造成资源泄漏。
                opened_img = Image.open(bytes_io)
                try:
                    to_gray = bool(getattr(self.api_service.config_manager, 'ocr_preprocess_to_gray', True))
                    max_width = int(getattr(self.api_service.config_manager, 'ocr_preprocess_max_width', 1200))
                    jpeg_quality = int(getattr(self.api_service.config_manager, 'ocr_preprocess_jpeg_quality', 85))

                    if to_gray:
                        img = opened_img.convert('L')
                    else:
                        img = opened_img.convert('RGB')
                finally:
                    # 先关闭原始打开的图像对象
                    try:
                        opened_img.close()
                    except Exception:
                        pass

                w, h = img.size
                if w > max_width:
                    new_h = int(h * (max_width / w))
                    try:
                        resample = Image.Resampling.LANCZOS
                        resized = img.resize((max_width, new_h), resample)
                    except Exception:
                        # Pillow older versions may not have Resampling.LANCZOS; fallback to default resize
                        resized = img.resize((max_width, new_h))
                    # 关闭 resize 前的中间图像
                    try:
                        img.close()
                    except Exception:
                        pass
                    img = resized

                buffered = BytesIO()
                img.save(buffered, format='JPEG', quality=jpeg_quality)
                b64_out = base64.b64encode(buffered.getvalue()).decode()
                return f"data:image/jpeg;base64,{b64_out}"
            finally:
                # 确保Image对象被释放
                if img:
                    try:
                        img.close()
                    except Exception:
                        pass
                bytes_io.close()
        except Exception as e:
            self.log_signal.emit(f"OCR预处理失败: {str(e)}，将使用原图进行识别。", True, "WARNING")
            return img_str
        finally:
            # 双重保险：确保buffered也被关闭
            if buffered:
                try:
                    buffered.close()
                except:
                    pass


    def evaluate_answer(self, img_str, prompt, current_question_config, dual_evaluation=False, score_diff_threshold: float = 10, ocr_text=""):
        """
        评估答案（重构后）。
        协调API调用和响应处理，支持单评和双评模式。
        新增: 支持OCR辅助评分

        Args:
            ocr_text: OCR识别的文本，如果为空则纯视觉评分
        """
        # 禁止同时存在图像和OCR文本作为双重输入（防止AI同时接收图像内容和OCR文本）
        if img_str and isinstance(img_str, str) and img_str.strip() and ocr_text and isinstance(ocr_text, str) and ocr_text.strip():
            error_msg = "AI不得同时接收图片和OCR文本作为双重输入，请选择一个模式（图像或OCR文本）"
            self.log_signal.emit(error_msg, True, "ERROR")
            self._set_error_state(error_msg)
            return None

        # 如果存在OCR文本，打印一条简短日志（只保留前50字符）以便调试
        if ocr_text and isinstance(ocr_text, str) and ocr_text.strip():
            snippet = ocr_text.replace('\n', ' ')[:50]
            self.log_signal.emit(f"OCR文本传入evaluate_answer（前50字符）: {snippet}", False, "DETAIL")

        # 初始化返回字段（避免并发/串行分支下引用未赋值）
        score1 = reasoning1 = scores1 = confidence1 = response_text1 = None
        score2 = reasoning2 = scores2 = confidence2 = response_text2 = None
        error1 = error2 = None

        # 单评：只调用第一个API
        if not dual_evaluation:
            score1, reasoning1, scores1, confidence1, response_text1, error1 = self._call_and_process_single_api(
                self.api_service.call_first_api,
                img_str,
                prompt,
                current_question_config,
                api_name="第一个API",
                ocr_text=ocr_text
            )
            if error1:
                self._set_error_state(error1)
                return None, error1, None, None, ""
            return score1, reasoning1, scores1, confidence1, response_text1

        # 双评：决定是否并发
        # - provider 相同：保持串行（降低触发限流/风控概率）
        # - provider 不同：并发调用（降低总耗时），并对第二个请求增加200-500ms随机延迟，避免同时起飞
        first_provider = None
        second_provider = None
        try:
            cm = getattr(self.api_service, 'config_manager', None)
            first_provider = getattr(cm, 'first_api_provider', None) if cm else None
            second_provider = getattr(cm, 'second_api_provider', None) if cm else None
        except Exception:
            first_provider = None
            second_provider = None

        providers_same = bool(first_provider and second_provider and str(first_provider) == str(second_provider))

        if providers_same:
            self.log_signal.emit(
                f"双评检测到相同provider({first_provider})，为降低限流风险保持串行调用...",
                False, "DETAIL"
            )

            score1, reasoning1, scores1, confidence1, response_text1, error1 = self._call_and_process_single_api(
                self.api_service.call_first_api,
                img_str,
                prompt,
                current_question_config,
                api_name="第一个API",
                ocr_text=ocr_text
            )
            if error1:
                self._set_error_state(error1)
                return None, error1, None, None, ""

            score2, reasoning2, scores2, confidence2, response_text2, error2 = self._call_and_process_single_api(
                self.api_service.call_second_api,
                img_str,
                prompt,
                current_question_config,
                api_name="第二个API",
                ocr_text=ocr_text
            )
        else:
            jitter_delay = random.uniform(0.2, 0.5)
            self.log_signal.emit(
                f"双评并发模式：provider不同({first_provider} vs {second_provider})，将并发调用；第二个请求延迟{jitter_delay:.2f}s。",
                False, "DETAIL"
            )

            def _call_api1():
                return self._call_and_process_single_api(
                    self.api_service.call_first_api,
                    img_str,
                    prompt,
                    current_question_config,
                    api_name="第一个API",
                    ocr_text=ocr_text
                )

            def _call_api2_with_delay():
                time.sleep(jitter_delay)
                return self._call_and_process_single_api(
                    self.api_service.call_second_api,
                    img_str,
                    prompt,
                    current_question_config,
                    api_name="第二个API",
                    ocr_text=ocr_text
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                future1 = executor.submit(_call_api1)
                future2 = executor.submit(_call_api2_with_delay)
                score1, reasoning1, scores1, confidence1, response_text1, error1 = future1.result()
                score2, reasoning2, scores2, confidence2, response_text2, error2 = future2.result()

                if error1:
                    self._set_error_state(error1)
                    return None, error1, None, None, ""

        # 保持与原逻辑一致：任意一方失败都中止，不做降级容错
        if error2:
            self._set_error_state(error2)
            return None, error2, None, None, ""

        # 处理双评结果
        final_score, combined_reasoning, combined_scores, combined_confidence, error_dual = self._handle_dual_evaluation(
            (score1, reasoning1, scores1, confidence1, response_text1),
            (score2, reasoning2, scores2, confidence2, response_text2),
            score_diff_threshold
        )
        if error_dual:
            # 双评特有的错误（如分差过大）需要设置线程状态
            self.completion_status = "threshold_exceeded"
            self.interrupt_reason = error_dual
            self.running = False
            return None, error_dual, None, None, ""

        # 双评模式成功时，合并两次API的原始响应
        combined_raw_response = f"API1:\n{response_text1}\n\nAPI2:\n{response_text2}"
        return final_score, combined_reasoning, combined_scores, combined_confidence, combined_raw_response

    def _perform_ocr_recognition(self, img_str, question_type='Subjective_PointBased_QA', ocr_quality_level='moderate'):
        """
        执行OCR识别（学生手写答案专用版本）
        
        🎯 设计原则（2025-12-12最终优化v2）:
        1. ✅ 纯净原文输出 - 完全尊重学生手写内容
        2. ✅ 完全忽略涂改 - 涂改行不出现在OCR文本中
        3. ✅ 纯文本识别 - 不使用LaTeX公式格式化
        4. ✅ 积分制质量检测 - 风险分数>=2时才要求人工介入
        5. ✅ 差异化阈值 - 根据题型调整质量要求
        6. ✅ 详细元数据 - 记录完整的置信度统计信息供调试
        
        适用场景:
        - 输入：已框定的单题答案区域截图
        - 内容：纯手写学生答案（无印刷文字）
        - 要求：高质量识别，容错率可根据题型调整
        
        Args:
            img_str: base64编码的图片字符串
            question_type: 题目类型，用于确定OCR质量阈值
            ocr_quality_level: OCR精度等级，可选值为 'relaxed'/'moderate'/'strict'，默认 'moderate'
        
        Returns:
            (ocr_text, meta_info)
            - ocr_text: 纯净的原文文本（不含涂改内容）
            - meta_info: 置信度统计 + 人工介入标记
        """
        self.log_signal.emit("开始 OCR 识别（学生手写答案专用模式）", False, "DETAIL")
        try:
            # 可选的OCR预处理（降采样 + 灰度）以提高识别稳定性和速度
            use_preprocess = False
            try:
                use_preprocess = bool(getattr(self.api_service.config_manager, 'ocr_preprocess_enabled', False))
            except Exception:
                use_preprocess = False

            if use_preprocess and img_str:
                try:
                    img_str = self._preprocess_image_for_ocr(img_str)
                    self.log_signal.emit("已对图片进行OCR预处理（降采样/灰度）", False, "DETAIL")
                except Exception as e_pre:
                    self.log_signal.emit(f"OCR预处理失败，使用原图进行识别: {str(e_pre)}", True, "WARNING")
            
            # 调用百度“手写文字识别”接口（保持旧方法名以兼容历史逻辑）
            # 传入 OCR 精度等级：strict 会启用 small 粒度
            # 使用统一重试机制（最多重试1次）
            data = None
            
            def _do_ocr_call():
                """内部OCR调用函数，供统一重试机制使用"""
                try:
                    try:
                        result_data, result_error = self.api_service.call_baidu_doc_analysis_structured(
                            img_str, ocr_quality_level=ocr_quality_level
                        )
                    except TypeError:
                        # 兼容旧版本 ApiService（如果用户回滚文件）
                        result_data, result_error = self.api_service.call_baidu_doc_analysis_structured(img_str)
                    
                    # 如果有错误或数据为空，抛出异常供重试机制处理
                    if result_error or not result_data:
                        raise RuntimeError(result_error or "OCR返回空数据")
                    
                    return result_data
                    
                except Exception as e:
                    # 统一异常格式
                    raise RuntimeError(f"调用百度OCR异常: {str(e)}")
            
            try:
                # 应用统一重试装饰器
                @unified_retry(
                    max_retries=1,
                    transient_error_checker=lambda e: self._is_transient_error(str(e)),
                    log_callback=self.log_signal.emit,
                    operation_name="OCR识别"
                )
                def _ocr_with_retry():
                    return _do_ocr_call()
                
                data = _ocr_with_retry()
                
            except Exception as e:
                # 所有重试都失败了
                error_msg = str(e)
                self.log_signal.emit(f"OCR识别失败（已重试1次）: {error_msg}", True, "ERROR")
                return "", {'manual_intervention': True, 'reason': f'OCR接口错误（已重试1次）: {error_msg}'}

            if not data:
                return "", {'manual_intervention': True, 'reason': 'OCR返回空数据'}

            # 📝 解析OCR结果（纯文本模式）
            results = data.get('results') or data.get('words_result') or []
            if not results:
                return "", {'manual_intervention': True, 'reason': 'OCR未返回任何文本行'}
            
            # 🎯 核心步骤1：提取所有文本行及其置信度（完全忽略涂改行）
            line_entries = []
            altered_count = 0  # 统计涂改行数量
            
            for item in results:
                # 🔍 严格的数据处理 - 任何异常立即停止（fail-fast）
                try:
                    # 检查item是否为dict（最基本的验证）
                    if not isinstance(item, dict):
                        raise ValueError(f"OCR返回项不是字典，类型为: {type(item)}")
                    
                    # 提取words字段
                    words_field = item.get('words')
                    
                    # 确定word_text
                    word_text = None
                    if isinstance(words_field, dict):
                        raw_word = words_field.get('word')
                        if raw_word is None:
                            raise ValueError("words字典中不存在'word'字段")
                        word_text = str(raw_word)  # 强制转换
                    elif isinstance(words_field, str):
                        word_text = words_field
                    else:
                        raise ValueError(f"words字段格式无效，既不是dict也不是str，类型为: {type(words_field)}")
                    
                    # 最终类型检查
                    if not isinstance(word_text, str):
                        raise ValueError(f"word_text转换失败，最终类型为: {type(word_text)}")
                    
                    # 检测涂改（涂改行仍然要跳过，因为这是学生的明确删除操作）
                    if '☰' in word_text:
                        altered_count += 1
                        self.log_signal.emit(f"检测到涂改行（学生删除标记），已忽略此行", False, "DETAIL")
                        continue  # 涂改是学生的明确意图，跳过是正确的
                    
                    # 严格的置信度提取
                    prob_block = None
                    if isinstance(words_field, dict):
                        prob_block = words_field.get('probability') or words_field.get('line_probability')
                    if prob_block is None:
                        prob_block = item.get('probability')
                    
                    # 置信度必须存在（对于有效识别）
                    # 兼容：少数情况下接口可能返回顶层 probability（若出现则兜底使用）
                    if prob_block is None and isinstance(data, dict):
                        top_prob = data.get('probability')
                        if isinstance(top_prob, dict):
                            prob_block = top_prob

                    if prob_block is None:
                        raise ValueError("本行OCR数据中不存在置信度信息")
                    
                    if not isinstance(prob_block, dict):
                        raise ValueError(f"置信度格式无效，类型为: {type(prob_block)}")
                    
                    # 提取并验证置信度值
                    avg_val = prob_block.get('average')
                    min_val = prob_block.get('min')
                    
                    if avg_val is None:
                        raise ValueError("置信度中不存在'average'字段")
                    
                    try:
                        avg_prob = float(avg_val)
                        if not (0 <= avg_prob <= 1):
                            raise ValueError(f"average置信度超出范围[0,1]: {avg_prob}")
                    except (ValueError, TypeError) as e:
                        raise ValueError(f"average转换为float失败: {e}")
                    
                    if min_val is not None:
                        try:
                            min_prob = float(min_val)
                            if not (0 <= min_prob <= 1):
                                raise ValueError(f"min置信度超出范围[0,1]: {min_prob}")
                        except (ValueError, TypeError) as e:
                            raise ValueError(f"min转换为float失败: {e}")
                    else:
                        min_prob = None
                    
                    # 所有检查通过，记录本行
                    line_entries.append({
                        'text': word_text,
                        'average': avg_prob,
                        'min': min_prob
                    })
                    
                except Exception as e:
                    # ❌ 任何单行数据异常都是严重问题，需要人工介入
                    error_msg = f"OCR数据格式异常（第{len(line_entries)+1}行）: {str(e)}"
                    self.log_signal.emit(error_msg, True, "ERROR")
                    # 立即停止并请求人工介入，不跳过
                    return "", {
                        'manual_intervention': True,
                        'reason': error_msg,
                        'detailed_reason': f"⚠️ OCR返回的数据格式异常，无法信任\n\n错误详情：\n{error_msg}\n\n建议：请人工查看该答题卡并手动评分"
                    }
            
            # 统计信息
            total_lines = len(line_entries)
            if altered_count > 0:
                self.log_signal.emit(f"共检测到 {altered_count} 行涂改内容，已全部忽略", False, "DETAIL")
            
            # 过滤出带置信度的行
            with_conf = [l for l in line_entries if l['average'] is not None]
            
            if total_lines == 0:
                return "", {'manual_intervention': True, 'reason': 'OCR返回无行信息'}

            if len(with_conf) == 0:
                return "", {'manual_intervention': True, 'reason': 'OCR未返回置信度，请检查接口参数'}

            # 🎯 核心步骤2：计算置信度统计（严格验证）
            try:
                # 严格提取所有有效的置信度值
                averages = []
                mins = []
                
                for l in with_conf:
                    avg = l.get('average')
                    # average必须存在且有效
                    if avg is None:
                        raise ValueError("某行置信度中不存在average值")
                    if not isinstance(avg, (int, float)):
                        raise ValueError(f"average不是数字类型: {type(avg)}")
                    if not (0 <= avg <= 1):
                        raise ValueError(f"average超出范围[0,1]: {avg}")
                    averages.append(avg)
                    
                    # min值可选但如果存在必须有效
                    min_val = l.get('min')
                    if min_val is not None:
                        if not isinstance(min_val, (int, float)):
                            raise ValueError(f"min不是数字类型: {type(min_val)}")
                        if not (0 <= min_val <= 1):
                            raise ValueError(f"min超出范围[0,1]: {min_val}")
                        mins.append(min_val)
                
                # 必须有足够的有效数据
                if not averages:
                    raise ValueError("没有有效的average置信度数据")
                
                # 计算统计值
                avg_conf = sum(averages) / len(averages)  # 必然不会除零
                
                # 验证结果合理性
                if not (0 <= avg_conf <= 1):
                    raise ValueError(f"计算结果avg_conf超出范围: {avg_conf}")
                
                min_conf = min(mins) if mins else (min(averages) if averages else None)
                if min_conf is None:
                    raise ValueError("无法计算最小置信度")
                if not (0 <= min_conf <= 1):
                    raise ValueError(f"计算结果min_conf超出范围: {min_conf}")
                    
            except Exception as e:
                # ❌ 置信度统计异常意味着无法信任OCR结果，必须人工介入
                error_msg = f"计算OCR置信度统计失败: {str(e)}"
                self.log_signal.emit(error_msg, True, "ERROR")
                return "", {
                    'manual_intervention': True,
                    'reason': error_msg,
                    'detailed_reason': f"⚠️ OCR置信度计算异常，无法信任识别结果\n\n错误详情：\n{error_msg}\n\n建议：答题卡清晰度不佳，请人工查看该答题卡并手动评分"
                }
            
            # 🎯 获取用户选择的质量等级和对应阈值（使用传入的参数）
            quality_level = ocr_quality_level
            # 转换UI文本到内部值（以防传入的是UI文本）
            quality_level = get_ocr_quality_internal_value(quality_level)
            
            # 获取该质量等级下的题型阈值
            type_thresholds = OCR_QUALITY_THRESHOLDS.get(quality_level, {}).get(
                question_type,
                OCR_QUALITY_THRESHOLDS['moderate']['Subjective_PointBased_QA']
            )
            cfg_avg_threshold = type_thresholds['avg']
            cfg_min_threshold = type_thresholds['min']
            cfg_low_ratio = type_thresholds['low_ratio']
            threshold_reason = type_thresholds['reason']
            
            # 统计低置信度行数
            low_count = sum(1 for l in with_conf if l['average'] is not None and l['average'] < cfg_min_threshold)
            low_ratio = low_count / len(with_conf) if len(with_conf) > 0 else 0

            # 🎯 核心步骤3：构造纯净原文OCR文本（不含任何标注）
            ocr_text_lines = []
            for entry in line_entries:
                text = entry['text']
                if text and text.strip():  # 只保留非空行
                    ocr_text_lines.append(text)
            
            # 最终OCR文本：纯净原文，尊重学生手写
            ocr_text = "\n".join(ocr_text_lines)

            # 决定是否需要人工介入
            manual = False
            reason = None
            if avg_conf < cfg_avg_threshold:
                manual = True
                reason = f"平均置信度过低: {avg_conf:.3f} < {cfg_avg_threshold}"
            elif min_conf < cfg_min_threshold:
                manual = True
                reason = f"存在行置信度过低: {min_conf:.3f} < {cfg_min_threshold}"
            elif low_ratio > cfg_low_ratio:
                manual = True
                reason = f"低置信度行占比过高: {low_ratio:.2%} > {cfg_low_ratio:.2%} (低行数: {low_count}/{len(with_conf)})"

            # 🎯 核心步骤4：积分制质量检测 - 自适应风险阈值策略
            risk_score = 0
            risk_details = []
            
            # 检测项1：平均置信度
            if avg_conf < cfg_avg_threshold:
                risk_score += 1
                risk_details.append(f"平均置信度{avg_conf:.1%} < {cfg_avg_threshold:.1%}")
            
            # 检测项2：最低行置信度
            if min_conf < cfg_min_threshold:
                risk_score += 1
                risk_details.append(f"最低行置信度{min_conf:.1%} < {cfg_min_threshold:.1%}")
            
            # 检测项3：低质量行占比
            if low_ratio > cfg_low_ratio:
                risk_score += 1
                risk_details.append(f"低质量行占比{low_ratio:.1%} > {cfg_low_ratio:.1%}")
            
            # 判定：根据总行数自适应调整风险触发门槛
            # 行数越少，统计样本越小，越需要严格控制（降低required_risk）
            if len(with_conf) <= 2:
                # 1-2行：单一指标不合格即触发（样本太少，不能容错）
                required_risk = 1
            elif len(with_conf) <= 4:
                # 3-4行：两个指标不合格才触发（中等容错）
                required_risk = 2
            else:
                # 5行+：标准积分制（充足样本，可适度容错）
                required_risk = 2
            
            manual = (risk_score >= required_risk)
            reason = None
            detailed_reason = None
            
            if manual:
                reason = f"OCR质量检测未通过（风险分数{risk_score}/3，阈值≥{required_risk}）：" + "、".join(risk_details)
                detailed_reason = (
                    f"⚠️ OCR识别质量不足，自动阅卷已暂停\n\n"
                    f"题型：{question_type}\n"
                    f"质量标准：{threshold_reason}\n\n"
                    f"问题详情：\n"
                    f"• 识别行数：{len(with_conf)} 行\n"
                    f"• 风险分数：{risk_score}/3（≥{required_risk}触发人工介入）\n"
                    f"• 平均置信度：{avg_conf:.1%}（要求≥{cfg_avg_threshold:.1%}）{'❌' if avg_conf < cfg_avg_threshold else '✓'}\n"
                    f"• 最低行置信度：{min_conf:.1%}（要求≥{cfg_min_threshold:.1%}）{'❌' if min_conf < cfg_min_threshold else '✓'}\n"
                    f"• 低质量行占比：{low_ratio:.1%}（要求≤{cfg_low_ratio:.1%}）{'❌' if low_ratio > cfg_low_ratio else '✓'}\n"
                    f"• 识别行数：{len(with_conf)} 行\n"
                    f"• 低质量行数：{low_count} 行\n\n"
                    f"原因：{' + '.join(risk_details)}\n\n"
                    f"建议：请人工查看该答题卡并手动评分"
                )

            # 🎯 核心步骤5：构建元数据（供调试和日志记录）
            meta = {
                'question_type': question_type,
                'thresholds_used': {
                    'avg': cfg_avg_threshold,
                    'min': cfg_min_threshold,
                    'low_ratio': cfg_low_ratio,
                    'reason': threshold_reason
                },
                'avg_confidence': float(avg_conf),
                'min_confidence': float(min_conf),
                'low_conf_count': int(low_count),
                'total_lines': int(total_lines),
                'valid_conf_lines': int(len(with_conf)),
                'low_conf_ratio': float(low_ratio),
                'altered_lines_ignored': int(altered_count),
                'risk_score': int(risk_score),
                'risk_details': risk_details,
                'manual_intervention': manual,
                'reason': reason,
                'detailed_reason': detailed_reason,
            }
            
            # 日志输出
            log_msg = (
                f"OCR识别完成[{question_type}]: {total_lines}行文本, "
                f"平均{avg_conf:.1%}, 最低{min_conf:.1%}, "
                f"低质量行{low_count}行(阈值≥{required_risk}, {low_ratio:.1%}), "
                f"风险分数{risk_score}/3"
            )
            if altered_count > 0:
                log_msg += f", 已忽略{altered_count}行涂改"
            
            self.log_signal.emit(log_msg, False, "DETAIL")
            
            # ⚠️ 如果需要人工介入，在日志中明确说明
            if manual:
                self.log_signal.emit(f"OCR质量检测未通过（风险{risk_score}/3≥{required_risk}）: {' + '.join(risk_details)}", True, "ERROR")
            
            return ocr_text, meta

        except Exception as e:
            error_detail = traceback.format_exc()
            self.log_signal.emit(f"OCR识别异常: {str(e)}\n{error_detail}", True, "ERROR")
            return "", {
                'manual_intervention': True,
                'reason': f'OCR处理异常: {str(e)}',
                'detailed_reason': f"⚠️ OCR处理过程发生异常\n\n错误详情：\n{str(e)}\n\n建议：请人工查看该答题卡并手动评分"
            }


    def _call_and_process_single_api(self, api_call_func, img_str, prompt, q_config, api_name="API", max_retries=2, ocr_text=""):
        """
        调用指定的API函数，并处理其响应。使用统一重试机制（最多重试1次），节省token和调用次数。

        Args:
            api_call_func: 要调用的API服务方法 (e.g., self.api_service.call_first_api)
            img_str: 图片base64字符串
            prompt: 提示词
            q_config: 当前题目配置
            api_name: 用于日志的API名称
            max_retries: 最大重试次数（已废弃，统一为1）
            ocr_text: OCR识别的文本，用于辅助评分

        Returns:
            一个元组 (score, reasoning, itemized_scores, confidence, response_text, error_message)
        """
        # 内部实现：单次API调用及响应处理
        def _do_api_call_and_process():
            self.log_signal.emit(f"正在调用{api_name}进行评分...", False, "DETAIL")
            # 如果有OCR文本，打印一条简短日志，便于调试
            if ocr_text and isinstance(ocr_text, str) and ocr_text.strip():
                snippet = ocr_text.replace('\n', ' ')[:80]
                self.log_signal.emit(f"传入OCR文本到{api_name}（前80字符）: {snippet}", False, "DETAIL")
            
            response_text, error_from_call = api_call_func(img_str, prompt, ocr_text)

            if error_from_call or not response_text:
                error_msg = f"{api_name}调用失败或响应为空: {error_from_call}"
                # 抛出异常供重试机制处理
                raise RuntimeError(error_msg)

            # 如果传入了 OCR 文本，则视为 OCR 模式（AI 评分可能不返回 student_answer_summary）
            ocr_mode_flag = bool(ocr_text and isinstance(ocr_text, str) and ocr_text.strip())
            success, result_data = self.process_api_response((response_text, None), q_config, ocr_mode=ocr_mode_flag, ocr_text=ocr_text)

            if success:
                score, reasoning, itemized_scores, confidence = result_data
                return score, reasoning, itemized_scores, confidence, response_text, None
            else:
                error_info = result_data
                # 检查是否为JSON解析错误（支持旧tuple格式和新的显式dict格式）
                is_json_parse_error = (
                    (isinstance(error_info, tuple) and len(error_info) >= 2 and error_info[0] == "json_parse_error") or
                    (isinstance(error_info, dict) and error_info.get('parse_error') and error_info.get('error_type') == 'json_parse_error')
                )

                # 检查是否为人工介入信号，若是则不重试，立即返回错误
                is_manual_intervention = (
                    isinstance(error_info, dict) and error_info.get('manual_intervention')
                )
                if is_manual_intervention:
                    # 安全地读取字段
                    error_msg = error_info.get('message') if isinstance(error_info, dict) else str(error_info)
                    self.log_signal.emit(f"{api_name}检测到人工介入请求: {error_msg}", True, "ERROR")
                    return None, None, None, None, response_text, error_msg

                if is_json_parse_error:
                    # JSON解析错误通常是模型输出格式问题（业务级），不重试以避免浪费调用次数
                    if isinstance(error_info, tuple):
                        error_msg = error_info[1] if len(error_info) > 1 else str(error_info)
                        raw_response = error_info[2] if len(error_info) > 2 else response_text
                    else:
                        if isinstance(error_info, dict):
                            error_msg = error_info.get('message', str(error_info))
                            raw_response = error_info.get('raw_response', response_text)
                        else:
                            error_msg = str(error_info)
                            raw_response = response_text

                    final_error_msg = f"{api_name}JSON解析失败（不重试以避免浪费调用）: {error_msg}"
                    self.log_signal.emit(final_error_msg, True, "ERROR")
                    return None, None, None, None, raw_response, final_error_msg
                else:
                    # 其他类型的处理失败，抛出异常供重试机制处理
                    raise RuntimeError(f"{api_name}处理失败: {error_info}")
        
        # 使用统一重试机制
        try:
            @unified_retry(
                max_retries=1,  # 统一为最多重试1次
                transient_error_checker=lambda e: self._is_transient_error(str(e)),
                log_callback=self.log_signal.emit,
                operation_name=api_name
            )
            def _api_with_retry():
                return _do_api_call_and_process()
            
            return _api_with_retry()
            
        except Exception as e:
            # 所有重试都失败了
            error_msg = str(e)
            self.log_signal.emit(f"{api_name}调用失败（已重试1次）: {error_msg}", True, "ERROR")
            return None, None, None, None, None, error_msg

    def _handle_dual_evaluation(self, result1, result2, score_diff_threshold):
        """
        处理双评逻辑，比较分数，合并结果。

        Args:
            result1: 第一个API的处理结果元组 (score, reasoning, itemized_scores, confidence, response_text)
            result2: 第二个API的处理结果元组 (score, reasoning, itemized_scores, confidence, response_text)
            score_diff_threshold: 分差阈值

        Returns:
            一个元组 (final_score, combined_reasoning, combined_scores, combined_confidence, error_message)
        """
        score1, reasoning1, itemized_scores1, confidence1, response_text1 = result1
        score2, reasoning2, itemized_scores2, confidence2, response_text2 = result2

        score_diff = abs(score1 - score2)
        self.log_signal.emit(f"API-1得分: {score1}, API-2得分: {score2}, 分差: {score_diff}", False, "INFO")

        if score_diff > score_diff_threshold:
            error_msg = f"双评分差过大: {score_diff:.2f} > {score_diff_threshold}"
            self.log_signal.emit(f"分差 {score_diff:.2f} 超过阈值 {score_diff_threshold}，停止运行", True, "ERROR")
            return None, None, None, None, error_msg

        avg_score = (score1 + score2) / 2.0

        summary1, basis1 = reasoning1 if isinstance(reasoning1, tuple) else (str(reasoning1), "")
        summary2, basis2 = reasoning2 if isinstance(reasoning2, tuple) else (str(reasoning2), "")

        dual_eval_details = {
            'is_dual': True,
            'api1_summary': summary1,
            'api1_basis': basis1,
            'api1_raw_score': score1,
            'api1_raw_response': response_text1,
            'api2_summary': summary2,
            'api2_basis': basis2,
            'api2_raw_score': score2,
            'api2_raw_response': response_text2,
            'score_difference': score_diff
        }

        itemized_scores_data_for_dual = {
            'api1_scores': itemized_scores1 if itemized_scores1 is not None else [],
            'api2_scores': itemized_scores2 if itemized_scores2 is not None else []
        }

        # 此版本暂时不启用置信度功能，今后如果需要再启用
        # combined_confidence = {
        #     'api1_confidence': confidence1,
        #     'api2_confidence': confidence2
        # }
        combined_confidence = {} # 置信度功能暂时停用

        return avg_score, dual_eval_details, itemized_scores_data_for_dual, combined_confidence, None

    def process_api_response(self, response, current_question_config, ocr_mode: bool = False, ocr_text: str = ""):
        """
        处理API响应，期望响应为JSON格式。重构后不再直接设置错误状态，而返回成功标志和结果。

        Args:
            response: API服务调用返回的元组 (response_text, error_message)
            current_question_config: 当前题目的配置

        Returns:
            (success, result):
                success (bool): 是否处理成功
                result: 如果成功，为 (score, reasoning_tuple, itemized_scores, confidence_data) 元组
                       如果失败，为错误信息字符串
        """
        response_text, error_from_api_call = response

        if error_from_api_call or not response_text:
            error_msg = f"API调用失败或响应为空: {error_from_api_call}"
            self.log_signal.emit(error_msg, True, "ERROR")
            return False, error_msg

        try:
            self.log_signal.emit("尝试解析API响应JSON...", False, "DETAIL")

            # 首先尝试直接解析
            data = None
            try:
                data = json.loads(response_text)
            except json.JSONDecodeError:
                # 如果直接解析失败，尝试提取JSON部分
                self.log_signal.emit("直接解析失败，尝试提取JSON部分...", False, "DETAIL")
                extracted_json = self._extract_json_from_text(response_text)
                if extracted_json:
                    try:
                        data = json.loads(extracted_json)
                        self.log_signal.emit("成功从响应中提取并解析JSON", False, "INFO")
                    except json.JSONDecodeError:
                        pass  # 仍然失败，继续到外层的异常处理

            if data is None:
                raise json.JSONDecodeError("无法解析响应为JSON", response_text, 0)

            # 验证必需字段是否存在；OCR模式下 student_answer_summary 为可选
            if ocr_mode:
                required_fields = ["scoring_basis", "itemized_scores"]
            else:
                required_fields = ["student_answer_summary", "scoring_basis", "itemized_scores"]

            missing_fields = [field for field in required_fields if field not in data]
            if missing_fields:
                error_msg = f"API响应JSON缺少必需字段: {', '.join(missing_fields)}"
                self.log_signal.emit(error_msg, True, "ERROR")
                return False, error_msg

            # 在OCR模式下，student_answer_summary 可选；若未返回则设为空字符串（或可在记录中使用OCR原文）
            student_answer_summary = data.get("student_answer_summary") if not ocr_mode else data.get("student_answer_summary", "")
            scoring_basis = data.get("scoring_basis", "未能提取评分依据")
            itemized_scores_from_json = data.get("itemized_scores")
            confidence_data = {}  # 置信度功能暂时停用

            if student_answer_summary:
                self.log_signal.emit(f"AI提取的学生答案摘要: {student_answer_summary}", False, "RESULT")
            else:
                if ocr_mode:
                    self.log_signal.emit(f"AI未返回 student_answer_summary（OCR模式），将以OCR原文为审计证据。", False, "RESULT")
                else:
                    self.log_signal.emit(f"AI提取的学生答案摘要为空。", True, "WARNING")
            self.log_signal.emit(f"AI评分依据: {scoring_basis}", False, "RESULT")

            # 检查是否为无法识别的情况，如果是则停止阅卷
            if ocr_mode and (not student_answer_summary):
                # OCR 模式下可能不会返回 student_answer_summary，尝试从 scoring_basis 中检测无法识别的信号
                unrecognizable_keywords = [
                    "无法", "无法识别", "字迹模糊", "无法辨认", "完全空白",
                    "图片内容完全无法识别", "字迹完全无法辨认", "未作答",
                    "学生未作答", "答题区域空白", "无任何作答痕迹"
                ]
                basis_lower = (scoring_basis or "").lower()
                if any(k in basis_lower for k in unrecognizable_keywords):
                    error_msg = f"学生答案图片无法识别（OCR模式），停止阅卷。请检查图片质量或手动处理。AI评分依据: {scoring_basis}"
                    self.log_signal.emit(error_msg, True, "ERROR")
                    return False, error_msg
            else:
                if self._is_unrecognizable_answer(student_answer_summary, itemized_scores_from_json):
                    error_msg = f"学生答案图片无法识别，停止阅卷。请检查图片质量或手动处理。AI反馈: {student_answer_summary}"
                    self.log_signal.emit(error_msg, True, "ERROR")
                    return False, error_msg

            # 检查AI是否明确请求人工介入（OCR相关的停止信号）
            manual_msg = self._detect_manual_intervention_feedback(student_answer_summary, scoring_basis)
            if manual_msg:
                error_msg = f"检测到AI请求人工介入: {manual_msg}"
                self.log_signal.emit(error_msg, True, "ERROR")
                # 在 OCR 模式下， AI 不返回摘要，此时将 OCR 原文传给 UI 便于人工复核
                display_text = student_answer_summary if student_answer_summary else (ocr_text if ocr_text else "")
                try:
                    self.manual_intervention_signal.emit(error_msg, display_text)
                except Exception:
                    pass
                # 返回带有标记的结构，便于上层立即停止且不重试；raw_feedback 同样使用 display_text
                return False, {'manual_intervention': True, 'message': error_msg, 'raw_feedback': display_text}

            # 检查AI是否在请求提供学生答案图片内容，如果是则停止阅卷并等待用户介入
            if self._is_ai_requesting_image_content(student_answer_summary, scoring_basis):
                error_msg = f"AI无法从图片中提取有效信息，停止阅卷并等待用户手动介入。AI反馈摘要: {student_answer_summary}"
                self.log_signal.emit(error_msg, True, "ERROR")
                return False, error_msg

            calculated_total_score = 0.0
            numeric_scores_list_for_return = []

            if itemized_scores_from_json is None or not isinstance(itemized_scores_from_json, list):
                error_msg = "API响应中'itemized_scores'缺失或格式错误 (应为列表)"
                self.log_signal.emit(error_msg, True, "ERROR")
                return False, error_msg

            if not itemized_scores_from_json:
                self.log_signal.emit("分项得分列表为空，判定总分为0。", False, "INFO")
                calculated_total_score = 0.0
                numeric_scores_list_for_return = []
            else:
                try:
                    # 使用 ScoreProcessor 处理分项得分
                    q_min_score = float(current_question_config.get('min_score', self.min_score))
                    q_max_score = float(current_question_config.get('max_score', self.max_score))
                    numeric_scores_list_for_return, calculated_total_score = ScoreProcessor.process_itemized_scores(
                        itemized_scores_from_json,
                        q_min_score,
                        q_max_score,
                        logger=self.log_signal.emit
                    )
                except ValueError as e_sanitize:
                    error_msg = f"API返回的分项得分 '{itemized_scores_from_json}' 包含无法解析的内容，解析失败 (错误: {e_sanitize})"
                    self.log_signal.emit(error_msg, True, "ERROR")
                    return False, error_msg

            self.log_signal.emit(f"根据itemized_scores计算得到的原始总分: {calculated_total_score}", False, "INFO")

            final_score = self._validate_and_finalize_score(calculated_total_score, current_question_config)

            if final_score is None:
                error_msg = "分数校验失败或超出范围"
                return False, error_msg

            reasoning_tuple = (student_answer_summary, data.get("scoring_basis", "未能提取评分依据"))

            result = (final_score, reasoning_tuple, numeric_scores_list_for_return, confidence_data)
            return True, result

        except json.JSONDecodeError as e_json:
            # 提供更详细的诊断信息
            response_preview = response_text[:500] if len(response_text) > 500 else response_text
            error_details = f"JSON解析错误详情: {str(e_json)}"
            content_analysis = self._analyze_response_content(response_text)

            error_msg = ("【API响应格式错误】模型返回的内容不是标准的JSON，无法解析。\n"
                         f"错误详情: {error_details}\n"
                         f"响应内容分析: {content_analysis}\n"
                         "可能原因：\n"
                         "1. 模型可能正忙或出现内部错误，导致输出了非结构化文本。\n"
                         "2. 您使用的模型可能不完全兼容当前Prompt的JSON输出要求。\n"
                         "3. 响应中包含了意外的格式字符或编码问题。\n"
                         "解决方案：系统将自动重试API调用。如果问题反复出现，建议更换模型或检查供应商服务状态。")
            self.log_signal.emit(f"{error_msg}\n原始响应(前500字符): '{response_preview}'", True, "ERROR")
            # 返回显式解析错误结构，包含原始响应，便于上层记录与诊断
            return False, {
                'parse_error': True,
                'error_type': 'json_parse_error',
                'message': error_msg,
                'raw_response': response_text
            }
        except (KeyError, IndexError) as e_key:
            error_msg = (f"【API响应结构错误】模型返回的JSON中缺少关键信息 (如: {str(e_key)})。\n"
                         f"可能原因：\n"
                         f"1. 模型未能完全遵循格式化输出的指令。\n"
                         f"2. API供应商可能更新了其响应结构。\n"
                         f"解决方案：这是程序需要处理的兼容性问题。请将此错误反馈给开发者。")
            self.log_signal.emit(f"{error_msg}\n完整响应: {response_text}", True, "ERROR")
            return False, error_msg
        except Exception as e_process:
            error_detail = traceback.format_exc()
            error_msg = f"处理API响应时发生未知错误: {str(e_process)}\n{error_detail}"
            self.log_signal.emit(error_msg, True, "ERROR")
            return False, error_msg

    def _validate_and_finalize_score(self, total_score_from_json: float, current_question_config):
        """
        验证从JSON中得到的总分，并进行最终处理（如范围校验，满分截断）。
        现在使用 ScoreProcessor 统一处理。
        """
        try:
            q_min_score = float(current_question_config.get('min_score', self.min_score))
            q_max_score = float(current_question_config.get('max_score', self.max_score))

            if not isinstance(total_score_from_json, (int, float)):
                error_msg = f"API返回的计算总分 '{total_score_from_json}' 不是有效数值。"
                self.log_signal.emit(error_msg, True, "ERROR")
                self._set_error_state(error_msg)
                return None

            # 使用 ScoreProcessor 进行范围校验（不进行四舍五入，因为此时还未到最终输入阶段）
            final_score = ScoreProcessor.validate_range(
                float(total_score_from_json),
                q_min_score,
                q_max_score,
                logger=self.log_signal.emit
            )

            self.log_signal.emit(f"AI原始总分: {total_score_from_json}, 校验后最终得分: {final_score}", False, "INFO")
            return final_score

        except Exception as e:
            error_detail = traceback.format_exc()
            error_msg = f"校验和处理分数时发生严重错误: {str(e)}\n{error_detail}"
            self.log_signal.emit(error_msg, True, "ERROR")
            self._set_error_state(error_msg)
            return None

    def _detect_manual_intervention_feedback(self, student_answer_summary: str, scoring_basis: str) -> Optional[str]:
        """
        检测AI返回的摘要或评分依据中是否包含指示需要人工介入的信号。
        返回匹配到的简短消息（str）或空字符串/None表示未检测到。
        """
        # 优先检测显式人工介入前缀（严格且明确）
        try:
            if isinstance(student_answer_summary, str):
                s_trim = student_answer_summary.strip()
                if s_trim.startswith('需人工介入:') or s_trim.startswith('需人工介入：'):
                    return '需人工介入'
        except Exception:
            pass

        if not student_answer_summary and not scoring_basis:
            return None

        combined = " ".join([str(student_answer_summary or ""), str(scoring_basis or "")])
        s = combined.lower()

        # 使用正则与同义词映射进行鲁棒检测（后处理层主判断）
        patterns = [
            r'需人工介入', r'人工介入', r'需人工复核', r'人工复核',
            r'无法(?:判定|评判|判断|评分|识别)', r'识别失败', r'识别错误', r'乱码', r'噪声太大',
            r'\bmanual intervention\b', r'\bneed manual\b', r'\bcannot (?:judge|score)\b', r'\bunclear\b', r'\brequires manual\b'
        ]

        for p in patterns:
            try:
                if re.search(p, s):
                    m = re.search(p, s)
                    return m.group(0) if m is not None else p
            except re.error:
                continue

        return None

    def _analyze_response_content(self, text):
        """
        分析响应内容，提供诊断信息。
        """
        if not text:
            return "响应为空"

        text = text.strip()
        length = len(text)

        # 检查是否包含JSON标记
        has_curly_braces = '{' in text and '}' in text
        has_square_brackets = '[' in text and ']' in text

        # 检查可能的格式问题
        issues = []
        if '\\n' in text or '\\t' in text or '\\r' in text:
            issues.append("包含转义字符")
        if text.count('{') != text.count('}'):
            issues.append("大括号不匹配")
        if text.count('[') != text.count(']'):
            issues.append("方括号不匹配")
        if 'data:' in text or 'base64,' in text:
            issues.append("可能包含图片数据")
        if length > 10000:
            issues.append("响应过长")

        # 分析开头和结尾
        start = text[:50] + "..." if len(text) > 50 else text
        end = "..." + text[-50:] if len(text) > 50 else text

        analysis = f"长度: {length}字符"
        if has_curly_braces:
            analysis += ", 包含大括号"
        if has_square_brackets:
            analysis += ", 包含方括号"
        if issues:
            analysis += f", 可能问题: {', '.join(issues)}"
        analysis += f"。开头: '{start}', 结尾: '{end}'"

        return analysis

    def _extract_json_from_text(self, text):
        """
        从文本中提取JSON字符串。
        增强版：处理常见的AI响应格式问题，包括多行JSON、格式问题、编码问题等。
        """
        import re
        try:
            # 清理常见的AI响应前缀和后缀
            text = text.strip()

            # 移除常见的markdown代码块标记
            text = re.sub(r'^```\s*json\s*', '', text, flags=re.IGNORECASE)
            text = re.sub(r'^```\s*', '', text)
            text = re.sub(r'```\s*$', '', text)

            # 移除可能的解释性文字前缀，如"以下是JSON响应："等
            # 查找可能的JSON开始位置
            start_pos = text.find('{')
            if start_pos != -1:
                # 检查前面是否有非JSON内容
                prefix = text[:start_pos].strip()
                if prefix and not prefix.endswith(':') and not prefix.endswith('：'):
                    text = text[start_pos:]
                else:
                    text = text[start_pos:]
            else:
                return None

            # 查找JSON结束位置（处理嵌套大括号）
            brace_count = 0
            end_pos = -1
            for i, char in enumerate(text):
                if char == '{':
                    brace_count += 1
                elif char == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        end_pos = i
                        break

            if end_pos != -1:
                candidate = text[:end_pos + 1]
                # 验证提取的JSON字符串
                try:
                    json.loads(candidate)
                    return candidate
                except json.JSONDecodeError:
                    pass  # 继续尝试其他方法

            # 回退到正则表达式方法
            # 使用正则表达式找到JSON对象，匹配最外层的{}，允许嵌套
            json_pattern = r'\{(?:[^{}]|{(?:[^{}]|{[^{}]*})*})*\}'
            match = re.search(json_pattern, text)
            if match:
                candidate = match.group(0)
                # 验证提取的JSON字符串
                try:
                    json.loads(candidate)
                    return candidate
                except json.JSONDecodeError:
                    pass

            # 最后尝试：如果文本看起来就是JSON（以{开头以}结尾），直接尝试解析
            if text.startswith('{') and text.endswith('}'):
                try:
                    json.loads(text)
                    return text
                except json.JSONDecodeError:
                    pass

            # 额外尝试：处理可能的编码或转义问题
            try:
                # 尝试修复常见的JSON格式问题
                fixed_text = text.replace('\\n', ' ').replace('\\t', ' ').replace('\\r', ' ')
                # 移除多余的转义
                fixed_text = re.sub(r'\\([^"\\nrt])', r'\1', fixed_text)
                # 处理中文标点符号
                fixed_text = fixed_text.replace('：', ':').replace('，', ',').replace('；', ';')
                # 处理可能的unicode转义
                fixed_text = fixed_text.encode().decode('unicode_escape')
                if fixed_text != text:
                    try:
                        json.loads(fixed_text)
                        return fixed_text
                    except json.JSONDecodeError:
                        pass
            except Exception:
                pass

            # 最后尝试：暴力清理所有非ASCII字符外的常见问题
            try:
                # 移除所有控制字符
                cleaned = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', text)
                # 确保引号正确
                cleaned = re.sub(r"'([^']*)'", r'"\1"', cleaned)  # 单引号转双引号（简单情况）
                if cleaned != text:
                    try:
                        json.loads(cleaned)
                        return cleaned
                    except json.JSONDecodeError:
                        pass
            except Exception:
                pass

            return None
        except Exception:
            return None

    def extract_reasoning(self, text):
        """
        此方法在新JSON Prompt方案下已不再直接使用。
        学生答案摘要和评分依据将从API返回的JSON中直接提取。
        保留此方法占位或用于旧逻辑兼容性（如果需要）。
        """
        # self.log_signal.emit("extract_reasoning被调用，但在新JSON方案下应通过JSON解析获取。", True)
        # return "（通过JSON获取学生答案摘要）", "（通过JSON获取评分依据）"
        pass # 或者直接返回None, None，或引发一个NotImplementedError

    def _perform_single_input(self, score_value, input_pos):
        """执行单次分数输入操作"""
        if not input_pos:
            self.log_signal.emit(f"输入位置未配置，无法输入分数 {score_value}", True, "ERROR")
            # self._set_error_state(f"输入位置未配置，无法输入分数 {score_value}") # 考虑是否需要，若需要则取消注释
            return False # 表示输入失败

        try:
            pyautogui.click(input_pos[0], input_pos[1])
            time.sleep(0.5)
            pyautogui.hotkey('ctrl', 'a')
            time.sleep(0.2)
            pyautogui.press('delete')
            time.sleep(0.2)
            pyautogui.write(str(score_value))
            time.sleep(0.5)
            return True # 表示输入成功
        except Exception as e:
            self.log_signal.emit(f"执行单次输入到 ({input_pos[0]},{input_pos[1]}) 出错: {str(e)}", True, "ERROR")
            # self._set_error_state(f"执行单次输入出错: {str(e)}") # 避免重复设置错误
            return False

    def input_score(self, final_score_to_input: float, default_score_pos, confirm_button_pos, current_question_config):
        """输入分数，根据模式选择单点或三步输入，并处理分数到0.5的倍数。"""
        try:
            input_successful = False
            current_processing_q_index = current_question_config.get('question_index', self.api_service.current_question_index)
            q_enable_three_step_scoring = current_question_config.get('enable_three_step_scoring', False)
            q_max_score = float(current_question_config.get('max_score', self.max_score)) # 确保是浮点数, 使用线程级默认最高分

            # 1. 获取用户配置的分数步长并使用 ScoreProcessor 统一处理
            score_step = getattr(self.api_service.config_manager, 'score_rounding_step', 0.5)
            q_min_score = float(current_question_config.get('min_score', self.min_score))
            
            # 使用 ScoreProcessor 进行完整的分数处理管道
            final_score_processed, process_log = ScoreProcessor.process_pipeline(
                final_score_to_input,
                q_min_score,
                q_max_score,
                score_step,
                logger=self.log_signal.emit
            )

            self.log_signal.emit(f"AI得分处理 (范围 [{q_min_score}-{q_max_score}]): {process_log}", False, "INFO")

            # 2. final_score_processed 已经经过完整的处理管道（清洗→四舍五入→范围校验），保证在有效范围内
            #    无需再次进行范围校验，ScoreProcessor 已经确保分数合法性

            # 3. 根据模式进行分数输入
            if (current_processing_q_index == 1 and
                q_enable_three_step_scoring and
                self.is_single_question_one_run):

                self.log_signal.emit(f"第一题启用三步分数输入模式，目标总分: {final_score_processed}", False, "INFO")

                # 获取三步打分的输入位置
                q_score_input_pos_step1 = current_question_config.get('score_input_pos_step1', None)
                q_score_input_pos_step2 = current_question_config.get('score_input_pos_step2', None)
                q_score_input_pos_step3 = current_question_config.get('score_input_pos_step3', None)

                if not all([q_score_input_pos_step1, q_score_input_pos_step2, q_score_input_pos_step3]):
                    self._set_error_state("三步打分模式启用，但部分输入位置未配置，阅卷中止。")
                    return

                # 三步打分分配：按最大给分顺序，每步最高20分（高中作文每步20分上限）
                # 先分配给第一步至多20分，再第二步，最后第三步
                step_max = 20.0  # 每步最高20分
                s1 = min(final_score_processed, step_max)
                s2 = min(max(0, final_score_processed - s1), step_max)
                s3 = max(0, final_score_processed - s1 - s2)

                # 由于 final_score_processed 和 score_per_step_cap 都是0.5的倍数, s1,s2,s3也都是
                total_split = s1 + s2 + s3
                self.log_signal.emit(f"三步拆分结果: s1={s1}, s2={s2}, s3={s3} (总和: {total_split})", False, "INFO")

                if not self._perform_single_input(s1, q_score_input_pos_step1):
                    self._set_error_state("三步打分输入失败 (步骤1)")
                    return
                if not self._perform_single_input(s2, q_score_input_pos_step2):
                    self._set_error_state("三步打分输入失败 (步骤2)")
                    return
                if not self._perform_single_input(s3, q_score_input_pos_step3):
                    self._set_error_state("三步打分输入失败 (步骤3)")
                    return
                input_successful = True

            else: # 标准单点输入
                self.log_signal.emit(f"标准单点输入模式 (题目 {current_processing_q_index})，得分: {final_score_processed}", False, "INFO")
                if not default_score_pos:
                    self._set_error_state(f"题目 {current_processing_q_index} 的分数输入位置未配置，阅卷中止。")
                    return
                if not self._perform_single_input(final_score_processed, default_score_pos):
                    self._set_error_state("分数输入失败")
                    return
                input_successful = True

            if input_successful:
                if not confirm_button_pos:
                    self._set_error_state("确认按钮位置未配置，阅卷中止。")
                    return
                pyautogui.click(confirm_button_pos[0], confirm_button_pos[1])
                time.sleep(0.5) # 轻微延时确保点击生效
                self.log_signal.emit(f"已输入总分: {final_score_processed} (题目 {current_processing_q_index}) 并点击确认", False, "INFO")
            # else 分支的错误已在各自的输入逻辑中通过 return 处理，或由 self.running 状态控制

        except Exception as e:
            error_detail = traceback.format_exc()
            self.log_signal.emit(f"输入分数过程中发生严重错误: {str(e)}\n{error_detail}", True, "ERROR")
            if self.running: # 避免在已停止时重复设置错误
                self._set_error_state(f"输入分数严重错误: {str(e)}")

    def record_grading_result(self, question_index, score, img_str, reasoning_data, itemized_scores_data, confidence_data, raw_ai_response=None, ocr_text="", ocr_meta=None):
        """记录阅卷结果，并发送信号 (重构后)"""
        try:
            # 提取评分细则前50字
            scoring_rubric_summary = "未配置"
            try:
                question_configs = self.parameters.get('question_configs', [])
                if question_configs and len(question_configs) > 0:
                    q_cfg = question_configs[0]
                    rubric = q_cfg.get('standard_answer', '')
                    if rubric and isinstance(rubric, str) and rubric.strip():
                        scoring_rubric_summary = rubric[:50] + ('...' if len(rubric) > 50 else '')
            except Exception:
                pass
            
            # 计算OCR置信度平均值
            ocr_avg_confidence = "未启用OCR"
            if ocr_meta and isinstance(ocr_meta, dict):
                avg_conf = ocr_meta.get('avg_confidence')
                if avg_conf is not None:
                    try:
                        ocr_avg_confidence = f"{float(avg_conf) * 100:.1f}%"
                    except (ValueError, TypeError):
                        ocr_avg_confidence = "计算失败"
            
            # 1. 构建基础记录字典
            record = {
                'timestamp': datetime.datetime.now().strftime('%Y年%m月%d日_%H点%M分%S秒'),
                'record_type': 'detail',
                'question_index': question_index,
                'total_score': score,
                'is_dual_evaluation_run': self.parameters.get('dual_evaluation', False),
                'total_questions_in_run': self.total_question_count_in_run,
                'scoring_rubric_summary': scoring_rubric_summary,
                'ocr_avg_confidence': ocr_avg_confidence,
            }

            # 2. 根据模式填充特定字段
            is_dual = isinstance(reasoning_data, dict) and reasoning_data.get('is_dual')

            record['is_dual_evaluation'] = is_dual

            # 判断是否处于 OCR 模式（依据是否有 OCR 文本）
            is_ocr_mode = bool(ocr_text and isinstance(ocr_text, str) and ocr_text.strip())

            if is_dual:
                # 双评模式：在 OCR 模式下不保存 API 返回的 student_answer_summary 字段，仅保留 OCR 原文作为审计证据
                base = {
                    'api1_scoring_basis': reasoning_data.get('api1_basis', 'AI未提供'),
                    'api1_raw_score': reasoning_data.get('api1_raw_score', 0.0),
                    'api1_raw_response': reasoning_data.get('api1_raw_response', 'AI未提供'),
                    'api2_scoring_basis': reasoning_data.get('api2_basis', 'AI未提供'),
                    'api2_raw_score': reasoning_data.get('api2_raw_score', 0.0),
                    'api2_raw_response': reasoning_data.get('api2_raw_response', 'AI未提供'),
                    'score_difference': reasoning_data.get('score_difference', 0.0),
                    'score_diff_threshold': self.parameters.get('score_diff_threshold', "AI未提供"),
                    'ocr_recognized_text': ocr_text if ocr_text else "未启用OCR或识别失败",
                    'ocr_confidence_meta': ocr_meta if ocr_meta else {},
                }
                if not is_ocr_mode:
                    base.update({
                        'api1_student_answer_summary': reasoning_data.get('api1_summary', 'AI未提供'),
                        'api2_student_answer_summary': reasoning_data.get('api2_summary', 'AI未提供'),
                    })
                record.update(base)
                if isinstance(itemized_scores_data, dict):
                    record['api1_itemized_scores'] = itemized_scores_data.get('api1_scores', [])
                    record['api2_itemized_scores'] = itemized_scores_data.get('api2_scores', [])

            elif isinstance(reasoning_data, dict) and reasoning_data.get('parse_error'):
                # 显式解析错误记录：使用结构化字段保存错误信息和原始响应，避免对字符串特征的脆弱判断
                parse_info = reasoning_data
                record.update({
                    'student_answer': "评分失败",
                    'reasoning_basis': parse_info.get('message', 'JSON解析错误'),
                    'raw_ai_response': parse_info.get('raw_response', 'AI未提供'),
                    'sub_scores': "AI未提供",
                    'ocr_recognized_text': ocr_text if ocr_text else "未启用OCR或识别失败",
                    'ocr_confidence_meta': ocr_meta if ocr_meta else {},
                })
                self.log_signal.emit(f"记录结果时检测到解析错误（显式模式），已保存原始AI响应", True, "ERROR")
            elif isinstance(reasoning_data, tuple) and len(reasoning_data) == 2:
                # 单评成功模式
                summary, basis = reasoning_data
                # 若 summary 为空且存在 OCR 文本，则使用 OCR 原文作为 student_answer 字段（便于审计）
                student_answer_for_record = summary if summary else (ocr_text if is_ocr_mode else "AI未提供")
                record.update({
                    'student_answer': student_answer_for_record,
                    'reasoning_basis': basis,
                    'sub_scores': str(itemized_scores_data) if itemized_scores_data is not None else "AI未提供",
                    'raw_ai_response': raw_ai_response if raw_ai_response is not None else "AI未提供",
                    'ocr_recognized_text': ocr_text if ocr_text else "未启用OCR或识别失败",
                    'ocr_confidence_meta': ocr_meta if ocr_meta else {},
                })

            else:
                # 错误或未知模式
                error_info = str(reasoning_data) if reasoning_data else "未知错误"
                record.update({
                    'student_answer': "评分失败",
                    'reasoning_basis': error_info,
                    'sub_scores': "AI未提供",
                    'ocr_recognized_text': ocr_text if ocr_text else "未启用OCR或识别失败",
                    'ocr_confidence_meta': ocr_meta if ocr_meta else {},
                })
                self.log_signal.emit(f"记录结果时遇到未预期的reasoning_data格式或错误: {error_info}", True, "ERROR")

            # 3. 发送信号
            self.record_signal.emit(record)
            self.log_signal.emit(f"第 {question_index} 题阅卷记录已发送。最终得分: {score}", False, "INFO")

        except Exception as e:
            error_detail = traceback.format_exc()
            self.log_signal.emit(f"记录阅卷结果时发生严重错误: {str(e)}\n{error_detail}", True, "ERROR")

    def generate_summary_record(self, cycle_number, dual_evaluation, score_diff_threshold, elapsed_time):
        """生成阅卷汇总记录"""
        # 单题模式：总题目数就是循环次数
        total_questions = cycle_number

        summary_record = {
            'timestamp': datetime.datetime.now().strftime('%Y年%m月%d日_%H点%M分%S秒'),
            'record_type': 'summary',
            'total_cycles': cycle_number,
            'total_questions_attempted': total_questions,
            'questions_completed': self.completed_count,
            'completion_status': self.completion_status,
            'interrupt_reason': self.interrupt_reason,
            'total_elapsed_time_seconds': elapsed_time,
            'dual_evaluation_enabled': dual_evaluation,
            'score_diff_threshold': score_diff_threshold if dual_evaluation else None,
            'first_model_id': self.first_model_id,
            'second_model_id': self.second_model_id if dual_evaluation else None,
            'is_single_question_one_run': self.is_single_question_one_run
        }

        # 将汇总记录发送给Application层
        self.record_signal.emit(summary_record)
        self.log_signal.emit("阅卷汇总记录已发送。", False, "INFO")
