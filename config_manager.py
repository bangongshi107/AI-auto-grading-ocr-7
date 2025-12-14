# --- START OF FILE config_manager.py ---

import configparser
import os
import sys
import appdirs

# ==============================================================================
#  OCR质量等级映射配置 (OCR Quality Level Mapping)
# ==============================================================================

OCR_QUALITY_UI_TO_INTERNAL = {
    '宽松': 'relaxed',
    '适度': 'moderate',
    '严格': 'strict'
}

OCR_QUALITY_INTERNAL_TO_UI = {
    'relaxed': '宽松',
    'moderate': '适度',
    'strict': '严格'
}

def get_ocr_quality_internal_value(ui_text: str) -> str:
    """将UI显示文本转换为内部标识"""
    return OCR_QUALITY_UI_TO_INTERNAL.get(ui_text, 'moderate')

def get_ocr_quality_ui_text(internal_value: str) -> str:
    """将内部标识转换为UI显示文本"""
    return OCR_QUALITY_INTERNAL_TO_UI.get(internal_value, '适度')

class ConfigManager:
    """配置管理器,负责保存和加载配置"""
    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ConfigManager, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if ConfigManager._initialized:
            return
        self.parser = configparser.ConfigParser(allow_no_value=True, interpolation=None)

        app_name = "AutoGraderApp"
        app_author = "Mr.Why"

        if getattr(sys, 'frozen', False):
            self.config_dir = appdirs.user_config_dir(app_name, app_author)
        else:
            self.config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "setting")

        self.config_file_path = os.path.join(self.config_dir, "config.ini")
        os.makedirs(self.config_dir, exist_ok=True)

        # OCR模式：使用索引而非文本，UI文本可随意修改
        # 索引 0 = 第一个选项（纯AI模式）
        # 索引 1 = 第二个选项（百度OCR模式）
        # 注意：索引顺序必须与UI文件中的下拉框选项顺序一致
        self.OCR_MODE_PURE_AI = 0
        self.OCR_MODE_BAIDU_OCR = 1
        
        # 为了兼容旧配置文件，保留文本到索引的映射
        self._legacy_ocr_text_to_index = {
            "纯AI识图阅卷": self.OCR_MODE_PURE_AI,
            "百度智能云OCR文字识别+AI评阅": self.OCR_MODE_BAIDU_OCR,
            "百度智能云文字识别+AI评阅": self.OCR_MODE_BAIDU_OCR,
            "百度OCR识别文字+AI评阅": self.OCR_MODE_BAIDU_OCR,
            "百度智能云手写OCR识别文字+AI评阅": self.OCR_MODE_BAIDU_OCR,
            "pure_ai": self.OCR_MODE_PURE_AI,
            "baidu_ocr": self.OCR_MODE_BAIDU_OCR,
        }

        self.max_questions = 7
        self._init_default_config()
        self.load_config()
        ConfigManager._initialized = True

    def _init_default_config(self):
        """初始化默认配置值
        
        注意：
        - first_api_provider 和 second_api_provider 是AI评分模型提供商
        - OCR模式（ocr_mode_index）是独立的工作模式选择：
          * 0 = 纯AI识图阅卷（AI直接读取图片）
          * 1 = 百度OCR模式（先OCR识别文字，再AI评分）
        - OCR和AI评分是两个独立的功能，不应混淆
        """
        # --- AI评分模型提供商配置 ---
        self.first_api_provider = "volcengine" # 默认使用火山引擎
        self.first_api_key = ""
        self.first_modelID = ""
        self.second_api_provider = "moonshot" # 默认使用 Moonshot
        self.second_api_key = ""
        self.second_modelID = ""
        
        self.dual_evaluation_enabled = False
        self.score_diff_threshold = 5
        self.subject = ""
        self.cycle_number = 1
        self.wait_time = 2

        # --- OCR工作模式配置（独立于AI模型选择）---
        # OCR配置（使用索引，0=纯AI，1=百度OCR）
        self.ocr_mode_index = self.OCR_MODE_PURE_AI  # 默认使用纯AI模式
        self.baidu_ocr_api_key = ""
        self.baidu_ocr_secret_key = ""
        # 持久化的百度OCR access_token 与过期时间（可选）
        self.baidu_ocr_access_token = ""
        self.baidu_ocr_token_expires_at = 0.0  # unix timestamp
        # 提前刷新 margin（秒），当剩余寿命小于该值时会触发刷新（默认 60s）
        self.baidu_ocr_token_refresh_margin = 60
        # OCR质量等级（新增）：relaxed/moderate/strict
        self.ocr_quality_level = "moderate"  # 默认适度
        # 分数步长（新增）：0.5或1
        self.score_rounding_step = 0.5  # 默认0.5步长
        # OCR质量阈值（默认，保留用于向后兼容）
        self.ocr_confidence_avg_threshold = 0.75
        self.ocr_confidence_min_threshold = 0.6
        self.ocr_confidence_low_line_ratio = 0.3
        
        self.question_configs = {}
        for i in range(1, self.max_questions + 1):
            is_q1 = (i == 1)
            self.question_configs[str(i)] = {
                'enabled': is_q1, # 第一题默认启用，其他默认禁用
                'score_input_pos': None,
                'confirm_button_pos': None,
                'standard_answer': "",
                'answer_area': None,
                'min_score': 0,
                'max_score': 100,
                'enable_next_button': False,
                'next_button_pos': None,
                'question_type': 'Subjective_PointBased_QA',
                'score_rounding_step': 0.5,  # 每题独立步长，默认0.5
                'ocr_mode_index': 0,  # 每题独立OCR模式，0=纯AI，1=百度OCR
                'ocr_quality_level': 'moderate',  # 每题独立OCR精度，relaxed/moderate/strict
            }
            if is_q1:
                self.question_configs[str(i)].update({
                    'enable_three_step_scoring': False,
                    'score_input_pos_step1': None,
                    'score_input_pos_step2': None,
                    'score_input_pos_step3': None
                })

    def load_config(self):
        """加载配置文件，如果不存在则创建默认配置"""
        if not os.path.exists(self.config_file_path):
            print(f"配置文件不存在，创建默认配置: {self.config_file_path}")
            self._save_config_to_file()
            return
        try:
            self.parser.read(self.config_file_path, encoding='utf-8')
        except configparser.Error as e:
            print(f"配置文件格式错误，使用默认配置: {e}")
            return
        self._safe_load_config()

    def _safe_load_config(self):
        """安全地加载配置，缺失项使用默认值"""
        # --- CHANGED: 加载 provider 而不是 url ---
        # 兼容旧/错误配置：允许 provider 字段写入 UI 文本（如“火山引擎 (推荐)”），自动映射为内部 provider_id（如 volcengine）。
        self.first_api_provider = self._normalize_ai_provider_value(
            self._get_config_safe('API', 'first_api_provider', "volcengine"),
            default_provider_id="volcengine",
            field_label="first_api_provider",
        )
        self.first_api_key = self._get_config_safe('API', 'first_api_key', "")
        self.first_modelID = self._get_config_safe('API', 'first_modelID', "")
        self.second_api_provider = self._normalize_ai_provider_value(
            self._get_config_safe('API', 'second_api_provider', "moonshot"),
            default_provider_id="moonshot",
            field_label="second_api_provider",
        )
        self.second_api_key = self._get_config_safe('API', 'second_api_key', "")
        self.second_modelID = self._get_config_safe('API', 'second_modelID', "")
        
        self.dual_evaluation_enabled = self._get_config_safe('DualEvaluation', 'enabled', False, bool)
        self.score_diff_threshold = self._get_config_safe('DualEvaluation', 'score_diff_threshold', 5, int)
        self.subject = self._get_config_safe('UI', 'subject', "")
        self.cycle_number = self._get_config_safe('Auto', 'cycle_number', 1, int)
        self.wait_time = self._get_config_safe('Auto', 'wait_time', 2, int)

        # 加载OCR配置（使用索引）
        ocr_mode_raw = self._get_config_safe('OCR', 'ocr_mode_index', self.OCR_MODE_PURE_AI)
        self.ocr_quality_level = self._get_config_safe('OCR', 'ocr_quality_level', 'moderate')
        self.score_rounding_step = float(self._get_config_safe('OCR', 'score_rounding_step', 0.5))
        
        # 兼容处理：如果是数字索引，直接使用；如果是旧版文本/标识符，转换为索引
        try:
            self.ocr_mode_index = int(ocr_mode_raw)
        except (ValueError, TypeError):
            # 旧版本可能保存的是文本或内部标识符，进行转换
            self.ocr_mode_index = self._legacy_ocr_text_to_index.get(str(ocr_mode_raw), self.OCR_MODE_PURE_AI)
        self.baidu_ocr_api_key = self._get_config_safe('OCR', 'baidu_ocr_api_key', "")
        self.baidu_ocr_secret_key = self._get_config_safe('OCR', 'baidu_ocr_secret_key', "")
        # Load persisted token (if any)
        try:
            self.baidu_ocr_access_token = self._get_config_safe('OCR', 'baidu_ocr_access_token', "")
            expires_raw = self._get_config_safe('OCR', 'baidu_ocr_token_expires_at', "0")
            try:
                self.baidu_ocr_token_expires_at = float(expires_raw)
            except Exception:
                self.baidu_ocr_token_expires_at = 0.0
            # token refresh margin
            try:
                self.baidu_ocr_token_refresh_margin = int(self._get_config_safe('OCR', 'baidu_ocr_token_refresh_margin', self.baidu_ocr_token_refresh_margin))
            except Exception:
                self.baidu_ocr_token_refresh_margin = 60
        except Exception:
            self.baidu_ocr_access_token = ""
            self.baidu_ocr_token_expires_at = 0.0
            self.baidu_ocr_token_refresh_margin = 60
        # 加载OCR质量阈值
        self.ocr_confidence_avg_threshold = float(self._get_config_safe('OCR', 'ocr_confidence_avg_threshold', self.ocr_confidence_avg_threshold))
        self.ocr_confidence_min_threshold = float(self._get_config_safe('OCR', 'ocr_confidence_min_threshold', self.ocr_confidence_min_threshold))
        self.ocr_confidence_low_line_ratio = float(self._get_config_safe('OCR', 'ocr_confidence_low_line_ratio', self.ocr_confidence_low_line_ratio))
        # 不再从配置文件读取/写入 UI 字号与字体族（移除用户自行调整字号的设定）
        
        for i in range(1, self.max_questions + 1):
            section_name = f'Question{i}'
            q_idx_str = str(i)
            
            # 第一题的 enabled 状态在加载后会被强制设为 True
            default_enabled = (i == 1)
            
            current_q_config = {
                'enabled': self._get_config_safe(section_name, 'enabled', default_enabled, bool),
                'score_input_pos': self._parse_position(self._get_config_safe(section_name, 'score_input', None)),
                'confirm_button_pos': self._parse_position(self._get_config_safe(section_name, 'confirm_button', None)),
                'standard_answer': self._get_config_safe(section_name, 'standard_answer', ""),
                'answer_area': self._parse_area(self._get_config_safe(section_name, 'answer_area', None)),
                'min_score': self._get_config_safe(section_name, 'min_score', 0, int),
                'max_score': self._get_config_safe(section_name, 'max_score', 100, int),
                'enable_next_button': self._get_config_safe(section_name, 'enable_next_button', False, bool),
                'next_button_pos': self._parse_position(self._get_config_safe(section_name, 'next_button_pos', None)),
                'question_type': self._get_config_safe(section_name, 'question_type', 'Subjective_PointBased_QA', str),
                'score_rounding_step': float(self._get_config_safe(section_name, 'score_rounding_step', '0.5')),  # 每题独立步长
                'ocr_mode_index': self._get_config_safe(section_name, 'ocr_mode_index', 0, int),  # 每题独立OCR模式
                'ocr_quality_level': self._get_config_safe(section_name, 'ocr_quality_level', 'moderate', str),  # 每题独立OCR精度
            }
            if i == 1:
                current_q_config['enable_three_step_scoring'] = self._get_config_safe(section_name, 'enable_three_step_scoring', False, bool)
                current_q_config['score_input_pos_step1'] = self._parse_position(self._get_config_safe(section_name, 'score_input_pos_step1', None))
                current_q_config['score_input_pos_step2'] = self._parse_position(self._get_config_safe(section_name, 'score_input_pos_step2', None))
                current_q_config['score_input_pos_step3'] = self._parse_position(self._get_config_safe(section_name, 'score_input_pos_step3', None))
            self.question_configs[q_idx_str] = current_q_config
        
        # 强制确保第一题始终启用
        if '1' in self.question_configs:
            self.question_configs['1']['enabled'] = True

    def _normalize_ai_provider_value(self, raw_value, default_provider_id: str, field_label: str) -> str:
        """将配置中的供应商字段标准化为内部 provider_id。

        兼容输入：
        - 内部ID：volcengine / moonshot / ...
        - UI文本：火山引擎 (推荐) / 月之暗面 / ...
        """
        if raw_value is None:
            return default_provider_id

        value = str(raw_value).strip()
        if not value:
            return default_provider_id

        # 延迟导入，避免潜在循环依赖与启动开销
        try:
            from api_service import PROVIDER_CONFIGS, get_provider_id_from_ui_text
        except Exception:
            # 如果映射不可用，至少返回原始值（后续由ApiService再兜底）
            return value

        # 已经是内部ID
        if value in PROVIDER_CONFIGS:
            return value

        # 尝试从 UI 文本映射回内部ID
        provider_id = get_provider_id_from_ui_text(value)
        if provider_id:
            return provider_id

        # 未知值：保留原值，方便UI显示用户填写的内容，但后续需要UI校验阻止启动
        try:
            print(f"[ConfigManager] 未识别的AI供应商配置({field_label}): {value}")
        except Exception:
            pass
        return value

    def _get_config_safe(self, section, option, default_value, value_type: type = str):
        """安全地获取配置值"""
        try:
            if not self.parser.has_section(section) or not self.parser.has_option(section, option):
                return default_value
            raw_val = self.parser.get(section, option)
            if value_type == str: return raw_val
            elif value_type == int: return int(raw_val) if raw_val and raw_val.strip() else default_value
            elif value_type == bool: return self.parser.getboolean(section, option)
            return default_value
        except (ValueError, TypeError):
            return default_value

    def _parse_position(self, pos_str):
        try:
            if not pos_str or not pos_str.strip(): return None
            x, y = map(int, map(str.strip, pos_str.split(',')))
            return (x, y)
        except (ValueError, AttributeError, TypeError): return None

    def _parse_area(self, area_str):
        try:
            if not area_str or not area_str.strip(): return None
            coords = [int(c.strip()) for c in area_str.split(',')]
            if len(coords) != 4: return None
            return {'x1': coords[0], 'y1': coords[1], 'x2': coords[2], 'y2': coords[3]}
        except (ValueError, TypeError): return None

    def update_config_in_memory(self, field_name, value):
        """更新内存中的配置项。"""
        try:
            self._update_memory_config(field_name, value)
        except Exception as e:
            print(f"ConfigManager: Error updating memory for {field_name}: {e}")

    def _update_memory_config(self, field_name, value):
        """更新内存中的配置"""
        # --- CHANGED: 更新 provider 而不是 url ---
        if field_name == 'first_api_provider': self.first_api_provider = str(value) if value else ""
        elif field_name == 'first_api_key': self.first_api_key = str(value) if value else ""
        elif field_name == 'first_modelID': self.first_modelID = str(value) if value else ""
        elif field_name == 'second_api_provider': self.second_api_provider = str(value) if value else ""
        elif field_name == 'second_api_key': self.second_api_key = str(value) if value else ""
        elif field_name == 'second_modelID': self.second_modelID = str(value) if value else ""
        elif field_name == 'subject': self.subject = str(value) if value else ""
        elif field_name == 'cycle_number': self.cycle_number = max(1, int(value)) if value else 1
        elif field_name == 'wait_time': self.wait_time = max(2, int(value)) if value else 2
        elif field_name == 'dual_evaluation_enabled': self.dual_evaluation_enabled = bool(value)
        elif field_name == 'score_diff_threshold': self.score_diff_threshold = max(1, int(value)) if value else 5
        elif field_name == 'ocr_mode_index': 
            try:
                self.ocr_mode_index = int(value) if value is not None else self.OCR_MODE_PURE_AI
            except (ValueError, TypeError):
                self.ocr_mode_index = self.OCR_MODE_PURE_AI
        elif field_name == 'ocr_mode':
            # 处理UI层传来的字符串值 ('pure_ai' 或 'baidu_ocr')
            if value == 'baidu_ocr':
                self.ocr_mode_index = self.OCR_MODE_BAIDU_OCR
            else:
                self.ocr_mode_index = self.OCR_MODE_PURE_AI
        elif field_name == 'baidu_ocr_api_key':
            self.baidu_ocr_api_key = str(value) if value else ""
            # API Key 变更时也清理旧 token
            self.baidu_ocr_access_token = ""
            self.baidu_ocr_token_expires_at = 0.0
            try:
                self._save_config_to_file()
            except Exception:
                pass
        elif field_name == 'baidu_ocr_secret_key':
            # 当 Secret Key 发生变化时，应清除已持久化的 access_token
            self.baidu_ocr_secret_key = str(value) if value else ""
            self.baidu_ocr_access_token = ""
            self.baidu_ocr_token_expires_at = 0.0
            # 立即持久化更改
            try:
                self._save_config_to_file()
            except Exception:
                pass
        elif field_name == 'ocr_quality_level': self.ocr_quality_level = str(value) if value else 'moderate'
        elif field_name == 'score_rounding_step':
            try:
                self.score_rounding_step = float(value) if value is not None else 0.5
            except (ValueError, TypeError):
                self.score_rounding_step = 0.5
        elif field_name.startswith('question_'): self._update_question_config_from_field_name(field_name, value)
        else:
            # 忽略未知的配置字段，比如旧的 'first_api_url'
            pass

    def _update_question_config_from_field_name(self, field_name, value):
        """从字段名解析并更新题目配置"""
        parts = field_name.split('_')
        if len(parts) < 3: return
        
        q_index, field_type = parts[1], '_'.join(parts[2:])
        if q_index not in self.question_configs: return

        if field_type == 'enabled': self.question_configs[q_index]['enabled'] = bool(value)
        elif field_type == 'standard_answer': self.question_configs[q_index]['standard_answer'] = str(value) if value else ""
        # 其他题目配置的更新逻辑保持不变...
        elif field_type == 'score_input_pos': self.question_configs[q_index]['score_input_pos'] = value
        elif field_type == 'confirm_button_pos': self.question_configs[q_index]['confirm_button_pos'] = value
        elif field_type == 'answer_area': self.question_configs[q_index]['answer_area'] = value
        elif field_type == 'min_score': self.question_configs[q_index]['min_score'] = int(value) if value is not None else 0
        elif field_type == 'max_score': self.question_configs[q_index]['max_score'] = int(value) if value is not None else 100
        elif field_type == 'enable_next_button': self.question_configs[q_index]['enable_next_button'] = bool(value)
        elif field_type == 'next_button_pos': self.question_configs[q_index]['next_button_pos'] = value
        elif field_type == 'question_type': self.question_configs[q_index]['question_type'] = str(value) if value else 'Subjective_PointBased_QA'
        elif field_type == 'score_rounding_step':  # 每题独立步长
            try:
                self.question_configs[q_index]['score_rounding_step'] = float(value) if value is not None else 0.5
            except (ValueError, TypeError):
                self.question_configs[q_index]['score_rounding_step'] = 0.5
        elif field_type == 'ocr_mode_index':  # 每题独立OCR模式
            try:
                self.question_configs[q_index]['ocr_mode_index'] = int(value) if value is not None else 0
            except (ValueError, TypeError):
                self.question_configs[q_index]['ocr_mode_index'] = 0
        elif field_type == 'ocr_quality_level':  # 每题独立OCR精度
            self.question_configs[q_index]['ocr_quality_level'] = str(value) if value else 'moderate'
        elif q_index == '1': # 仅第一题
            if field_type == 'enable_three_step_scoring': self.question_configs[q_index]['enable_three_step_scoring'] = bool(value)
            elif field_type == 'score_input_pos_step1': self.question_configs[q_index]['score_input_pos_step1'] = value
            elif field_type == 'score_input_pos_step2': self.question_configs[q_index]['score_input_pos_step2'] = value
            elif field_type == 'score_input_pos_step3': self.question_configs[q_index]['score_input_pos_step3'] = value

    def update_question_config(self, question_index, field_type, value):
        field_name = f"question_{question_index}_{field_type}"
        self._update_memory_config(field_name, value)

    def save_all_configs_to_file(self):
        return self._save_config_to_file()

    def _save_config_to_file(self):
        """将内存中的配置保存到文件"""
        try:
            config = configparser.ConfigParser(interpolation=None)
            
            # --- CHANGED: 保存 provider 而不是 url ---
            config['API'] = {
                'first_api_provider': str(self.first_api_provider),
                'first_api_key': str(self.first_api_key),
                'first_modelID': str(self.first_modelID),
                'second_api_provider': str(self.second_api_provider),
                'second_api_key': str(self.second_api_key),
                'second_modelID': str(self.second_modelID),
            }
            config['UI'] = {'subject': str(self.subject)}
            config['Auto'] = {'cycle_number': str(self.cycle_number), 'wait_time': str(self.wait_time)}
            config['DualEvaluation'] = {'enabled': str(self.dual_evaluation_enabled), 'score_diff_threshold': str(self.score_diff_threshold)}
            # 保存索引值（与UI文本无关）
            config['OCR'] = {
                'ocr_mode_index': str(self.ocr_mode_index),
                'baidu_ocr_api_key': str(self.baidu_ocr_api_key),
                'baidu_ocr_secret_key': str(self.baidu_ocr_secret_key),
                'ocr_quality_level': str(self.ocr_quality_level),
                'score_rounding_step': str(self.score_rounding_step),
                'ocr_confidence_avg_threshold': str(self.ocr_confidence_avg_threshold),
                'ocr_confidence_min_threshold': str(self.ocr_confidence_min_threshold),
                'ocr_confidence_low_line_ratio': str(self.ocr_confidence_low_line_ratio),
                # 持久化 token（注意：出于安全考虑，不建议在多用户环境下长期保存）
                'baidu_ocr_access_token': str(self.baidu_ocr_access_token),
                'baidu_ocr_token_expires_at': str(self.baidu_ocr_token_expires_at),
                'baidu_ocr_token_refresh_margin': str(self.baidu_ocr_token_refresh_margin),
            }
            
            for i in range(1, self.max_questions + 1):
                section_name = f'Question{i}'
                q_idx_str = str(i)
                q_config = self.question_configs[q_idx_str]
                
                is_enabled_for_saving = q_config['enabled']
                if q_idx_str == '1': is_enabled_for_saving = True

                section_data = {
                    'enabled': str(is_enabled_for_saving),
                    'standard_answer': q_config['standard_answer'],
                    'min_score': str(q_config['min_score']),
                    'max_score': str(q_config['max_score']),
                    'enable_next_button': str(q_config['enable_next_button']),
                    'question_type': q_config.get('question_type', 'Subjective_PointBased_QA'),
                    'score_rounding_step': str(q_config.get('score_rounding_step', 0.5)),  # 每题独立步长
                    'ocr_mode_index': str(q_config.get('ocr_mode_index', 0)),  # 每题独立OCR模式
                    'ocr_quality_level': q_config.get('ocr_quality_level', 'moderate'),  # 每题独立OCR精度
                    'score_input': f"{q_config['score_input_pos'][0]},{q_config['score_input_pos'][1]}" if q_config['score_input_pos'] else "",
                    'confirm_button': f"{q_config['confirm_button_pos'][0]},{q_config['confirm_button_pos'][1]}" if q_config['confirm_button_pos'] else "",
                    'next_button_pos': f"{q_config['next_button_pos'][0]},{q_config['next_button_pos'][1]}" if q_config['next_button_pos'] else "",
                    'answer_area': f"{q_config['answer_area']['x1']},{q_config['answer_area']['y1']},{q_config['answer_area']['x2']},{q_config['answer_area']['y2']}" if q_config['answer_area'] else "",
                }
                
                if q_idx_str == '1':
                    section_data['enable_three_step_scoring'] = str(q_config.get('enable_three_step_scoring', False))
                    pos1 = q_config.get('score_input_pos_step1')
                    section_data['score_input_pos_step1'] = f"{pos1[0]},{pos1[1]}" if pos1 else ""
                    pos2 = q_config.get('score_input_pos_step2')
                    section_data['score_input_pos_step2'] = f"{pos2[0]},{pos2[1]}" if pos2 else ""
                    pos3 = q_config.get('score_input_pos_step3')
                    section_data['score_input_pos_step3'] = f"{pos3[0]},{pos3[1]}" if pos3 else ""
                
                config[section_name] = section_data
            
            with open(self.config_file_path, 'w', encoding='utf-8') as f:
                config.write(f)
            return True
        except Exception as e:
            print(f"保存配置文件失败: {e}")
            return False

    def get_enabled_questions(self):
        return [i for i in range(1, self.max_questions + 1) if self.question_configs.get(str(i), {}).get('enabled', False)]

    def get_question_config(self, question_index):
        return self.question_configs.get(str(question_index), {'enabled': False})

    def is_baidu_ocr_mode(self):
        """判断当前是否为百度OCR模式"""
        return self.ocr_mode_index == self.OCR_MODE_BAIDU_OCR
    
    @property
    def ocr_mode(self):
        """返回OCR模式的内部标识字符串"""
        return "baidu_ocr" if self.ocr_mode_index == self.OCR_MODE_BAIDU_OCR else "pure_ai"
    
    def _smart_recognize_ocr_mode(self, ui_text):
        """智能识别OCR模式，支持模糊匹配UI文本"""
        if not ui_text:
            return "pure_ai"
        
        text_lower = ui_text.lower().strip()
        
        # 精确匹配内部标识
        if text_lower in ['pure_ai', 'baidu_ocr']:
            return text_lower
        
        # 模糊匹配中文文本
        if any(keyword in text_lower for keyword in ['百度', 'baidu', 'ocr', '识别']):
            return "baidu_ocr"
        
        if any(keyword in text_lower for keyword in ['纯ai', 'ai识图', '纯识图']):
            return "pure_ai"
        
        # 默认返回纯AI模式
        return "pure_ai"
    
    def check_required_settings(self):
        # 简化检查，MainWindow将负责UI层面的验证提示
        if not self.first_api_key or not self.first_modelID or not self.first_api_provider:
            return False
        if self.dual_evaluation_enabled and (not self.second_api_key or not self.second_modelID or not self.second_api_provider):
            return False
        return True

# --- END OF FILE config_manager.py ---
