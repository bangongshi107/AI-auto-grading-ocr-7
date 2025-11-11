# --- START OF FILE config_manager.py ---

import configparser
import os
import sys
import appdirs

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
        self.parser = configparser.ConfigParser(allow_no_value=True)
        
        app_name = "AutoGraderApp"
        app_author = "Mr.Why"

        if getattr(sys, 'frozen', False):
            self.config_dir = appdirs.user_config_dir(app_name, app_author)
        else:
            self.config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "setting")
        
        self.config_file_path = os.path.join(self.config_dir, "config.ini")
        os.makedirs(self.config_dir, exist_ok=True)
        
        self.max_questions = 4
        self._init_default_config()
        self.load_config()
        ConfigManager._initialized = True

    def _init_default_config(self):
        """初始化默认配置值"""
        # --- CHANGED: URL 字段被 provider 字段替换 ---
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
        self.first_api_provider = self._get_config_safe('API', 'first_api_provider', "volcengine")
        self.first_api_key = self._get_config_safe('API', 'first_api_key', "")
        self.first_modelID = self._get_config_safe('API', 'first_modelID', "")
        self.second_api_provider = self._get_config_safe('API', 'second_api_provider', "moonshot")
        self.second_api_key = self._get_config_safe('API', 'second_api_key', "")
        self.second_modelID = self._get_config_safe('API', 'second_modelID', "")
        
        self.dual_evaluation_enabled = self._get_config_safe('DualEvaluation', 'enabled', False, bool)
        self.score_diff_threshold = self._get_config_safe('DualEvaluation', 'score_diff_threshold', 5, int)
        self.subject = self._get_config_safe('UI', 'subject', "")
        self.cycle_number = self._get_config_safe('Auto', 'cycle_number', 1, int)
        self.wait_time = self._get_config_safe('Auto', 'wait_time', 2, int)
        
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
                'question_type': self._get_config_safe(section_name, 'question_type', 'Subjective_PointBased_QA', str)
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

    def _get_config_safe(self, section, option, default_value, value_type=str):
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
            config = configparser.ConfigParser()
            
            # --- CHANGED: 保存 provider 而不是 url ---
            config['API'] = {
                'first_api_provider': self.first_api_provider,
                'first_api_key': self.first_api_key,
                'first_modelID': self.first_modelID,
                'second_api_provider': self.second_api_provider,
                'second_api_key': self.second_api_key,
                'second_modelID': self.second_modelID,
            }
            config['UI'] = {'subject': self.subject}
            config['Auto'] = {'cycle_number': str(self.cycle_number), 'wait_time': str(self.wait_time)}
            config['DualEvaluation'] = {'enabled': str(self.dual_evaluation_enabled), 'score_diff_threshold': str(self.score_diff_threshold)}
            
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

    def check_required_settings(self):
        # 简化检查，MainWindow将负责UI层面的验证提示
        if not self.first_api_key or not self.first_modelID or not self.first_api_provider:
            return False
        if self.dual_evaluation_enabled and (not self.second_api_key or not self.second_modelID or not self.second_api_provider):
            return False
        return True

# --- END OF FILE config_manager.py ---
