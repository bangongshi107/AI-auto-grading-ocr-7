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
from typing import Optional
from threading import Lock
from threading import Lock

# 导入OCR配置函数
from config_manager import get_ocr_quality_internal_value

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

def round_to_step(value: float, step: float) -> float:
    """
    将数值四舍五入到指定步长的倍数。
    
    Args:
        value: 要四舍五入的数值
        step: 步长（如0.5或1）
    
    Examples:
        round_to_step(7.3, 0.5) -> 7.5
        round_to_step(7.3, 1.0) -> 7.0
        round_to_step(7.8, 0.5) -> 8.0
    """
    if step <= 0:
        return value
    return round(value / step) * step


def round_to_nearest_half(value: float) -> float:
    """
    将数值四舍五入到最接近的0.5的倍数（保留用于向后兼容）。
    例如: 7.2 -> 7.0, 7.25 -> 7.5, 7.6 -> 7.5, 7.75 -> 8.0
    （保留用于向后兼容）
    """
    return round_to_step(value, 0.5)


def sanitize_score(val):
    """
    清洗和标准化分数输入，确保返回有效的浮点数。
    如果无法提取有效数字，抛出 ValueError 以确保评分准确性。
    """
    if isinstance(val, (int, float)):
        return float(val)
    
    # 尝试从字符串中提取数字
    try:
        import re
        # 提取浮点数
        match = re.search(r'-?\d+\.?\d*', str(val))
        if match:
            return float(match.group())
    except Exception:
        pass
    
    raise ValueError(f"无法将 {val} 转换为有效的分数")


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
        
        # =================================================================
        # P0修复：线程安全与并发问题
        # =================================================================
        # 添加线程锁保护共享资源的并发访问
        self._params_lock = Lock()  # 保护self.parameters
        self._state_lock = Lock()   # 保护completion_status等状态变量
        self._temp_resources = []   # 追踪临时资源（图片对象等）以便清理

    def _get_common_system_message(self, ocr_mode=False):
        """
        返回通用的AI系统提示词。
        
        Args:
            ocr_mode: 是否为OCR模式（OCR模式不需要处理图片涂改等信息）
        
        Returns:
            str: 系统提示词
        """
        subject = "通用"
        
        # 尝试从config_manager获取科目
        subject_from_config = None
        if self.config_manager:
            try:
                subject_from_config = self.config_manager.get_setting('subject')
            except Exception:
                pass
        
        # 如果配置中的科目有效（非空、非纯空格），则使用配置中的科目
        if subject_from_config and isinstance(subject_from_config, str) and subject_from_config.strip():
            subject = subject_from_config.strip()
        
        if ocr_mode:
            # OCR纯文本模式 - 独立完整的提示词
            return (
                f"你是一位经验丰富、严谨细致的【{subject}】资深阅卷老师。"
                "你的核心任务是：根据【评分细则】和【题目类型说明】，对OCR识别的学生答案文本进行准确评分。"
                "请严格按照给定的JSON格式输出分析结果。\n\n"
                "【重要】你当前处于OCR纯文本评分模式，只能使用系统提供的OCR识别文本进行评分。\n\n"
                "在整个评分过程中，请严格遵守以下【评分总则】：\n"
                "1.  【严格依据细则】：你的所有评分判断【必须且仅能严格依据】【评分细则】中明确列出的每一个【得分点/答案要点/关键步骤/评估维度】（具体称呼依据题目类型而定）的标准和给分说明。严禁对评分细则进行任何形式的补充、推测、联想或超出细则范围进行给分或不给分。\n"
                "2.  【仅限文本内容】：你的评分判断【仅能依据】OCR识别的文本内容。严禁根据文本以外的任何信息（包括你对该学科知识的掌握、对评分细则的记忆或普遍常识）来猜测、臆断或虚构学生可能想表达的答案。\n"
                "3.  【关于扣分】：评分主要依据学生达到得分点的程度给分。**然而，如果【评分细则】中明确包含\"扣X分\"的指令（例如：'每处过度解读扣0.5分'，'关键词误译扣2分'等），你必须严格执行这些扣分指令。** 请在`scoring_basis`的\"判断与理由\"中清晰说明扣分的原因和依据，并在最终的`得分`或`itemized_scores`中体现扣分后的结果。\n"
                "4.  【错误处理】：若遇到以下任何情况，请立即报错并停止评分：\n"
                "    - 文本中出现明显的自我矛盾或修正痕迹（例如'选A，不对，应该选B'），请报错：【报错】文本存在自我矛盾，需人工介入\n"
                "    - OCR文本中包含大量噪声符号导致无法理解学生答案，请报错：【报错】文本噪声过多，需人工介入\n"
                "    - OCR文本逻辑明显不通（例如算式不成立、步骤跳跃严重），请报错：【报错】文本逻辑异常，需人工介入\n"
                "    - 遇到任何其他不确定情况，优先报错而非猜测。\n\n"
                "【特殊情况处理】：\n若OCR文本为空或完全无法理解，请按以下规则填充JSON：\n"
                "    - `student_answer_summary`: 明确注明具体情况，例如：\"OCR未识别到有效文本。\"，\"文本内容与题目要求完全无关。\"\n"
                "    - `scoring_basis`: 简要说明此判断的依据，例如：\"OCR文本为空。\"，\"文本内容不符合题目要求。\"\n"
                "    - `itemized_scores`:\n"
                "        - 对于按【得分点/答案要点/关键步骤】给分的题型，应输出一个与【评分细则】中预设的相应条目数量相同长度的全零列表（例如，若细则有3个得分点，则输出 `[0, 0, 0]`）。\n"
                "        - 对于整体评估的开放题型，应输出 `[0]`。\n"
            )
        else:
            # 纯AI视觉模式 - 独立完整的提示词
            return (
                f"你是一位经验丰富、严谨细致的【{subject}】资深阅卷老师。"
                "你的核心任务是：根据【评分细则】和【题目类型说明】，对学生答案的图片内容进行深入分析和准确评分。"
                "请严格按照给定的JSON格式输出分析结果。\n\n"
                "在整个评分过程中，请严格遵守以下【评分总则】：\n"
                "1.  【关于涂改】：学生在答案文字上所作的横线、斜线、删除线等涂改标记，均视为学生主动删除的内容，此部分不参与评分。"
                "学生在涂改后通常会在旁边、上方或下方补写新答案。涂改标记（如删除线）作废原内容，未被涂改标记覆盖的文字视为有效答案。"
                "若补充内容清晰可辨且与题目相关，应将其视为学生最终答案并纳入评分范围。\n"
                "2.  【严格依据细则】：你的所有评分判断【必须且仅能严格依据】【评分细则】中明确列出的每一个【得分点/答案要点/关键步骤/评估维度】（具体称呼依据题目类型而定）的标准和给分说明。严禁对评分细则进行任何形式的补充、推测、联想或超出细则范围进行给分或不给分。\n"
                "3.  【仅限图像内容】：你的评分判断【仅能依据】从学生答题卡图片中真实可辨识的手写内容。严禁根据图片以外的任何信息（包括你对该学科知识的掌握、对评分细则的记忆或普遍常识）来猜测、臆断或虚构学生可能想表达的答案。\n"
                "4.  【关于扣分】：评分主要依据学生达到得分点的程度给分。**然而，如果【评分细则】中明确包含\"扣X分\"的指令（例如：'每处过度解读扣0.5分'，'关键词误译扣2分'等），你必须严格执行这些扣分指令。** 请在`scoring_basis`的\"判断与理由\"中清晰说明扣分的原因和依据，并在最终的`得分`或`itemized_scores`中体现扣分后的结果。\n\n"
                "【特殊情况处理】：\n若学生答案图片完全空白、字迹完全无法辨认，或所写内容与题目要求完全无关，请按以下规则填充JSON：\n"
                "    - `student_answer_summary`: 明确注明具体情况，例如：\"学生未作答。\"，\"图片内容完全无法识别，字迹模糊不清。\"，\"学生答案内容与题目要求完全不符。\"\n"
                "    - `scoring_basis`: 简要说明此判断的依据，例如：\"答题区域空白，无任何作答痕迹。\"\n"
                "    - `itemized_scores`:\n"
                "        - 对于按【得分点/答案要点/关键步骤】给分的题型，应输出一个与【评分细则】中预设的相应条目数量相同长度的全零列表（例如，若细则有3个得分点，则输出 `[0, 0, 0]`）。\n"
                "        - 对于整体评估的开放题型，应输出 `[0]`。\n"
                "    - `recognition_confidence`: {\"score\": \"1\", \"reason\": \"[对应上述特殊情况的理由，例如：图片空白或字迹完全无法识别。]\"}\n"
            )


    def _build_objective_fillintheblank_prompt(self, standard_answer_rubric, ocr_mode=False):
        prompt_json = {
          "system_message": self._get_common_system_message(),
          "user_task": {
            "task_description": "请分析以下学生答案图片（客观填空题），并根据下方提供的评分细则给出评分。",
            "question_type_specific_instructions": "【题目类型：客观填空题】\n请仔细阅读【评分细则】中对【每一个填空项/答案要点】的具体标准答案、允许的表达方式及对应的分值。\n你的核心任务是判断学生对每个【填空项/答案要点】的回答是否符合细则要求。请严格遵照【评分细则】中关于答案细节（例如：格式、准确性等）的具体规定进行给分。若【评分细则】中包含灵活给分说明（如“意思对即可”），请在评分依据中体现你的理解。",
            "scoring_rubric_placeholder": standard_answer_rubric,
            "student_answer_image_placeholder": "（图片内容会通过API的其他方式传入，这里仅为逻辑占位）",
            "student_answer_text_placeholder": "（OCR文本会通过API的其他方式传入，此字段仅为逻辑占位）",
            "output_format_specification": {
              "description": "请严格按照以下JSON格式返回结果，不要包含任何额外的解释性文字在JSON结构之外。确保输出的是有效的JSON，不要添加任何前缀、后缀、代码块标记或自然语言描述。直接输出JSON对象。",
              "format": {
                "student_answer_summary": "【请在此处对图片中的学生手写答案进行**中立、客观、不带任何主观判断**的【核心内容概括】（例如：“学生答案的主要内容为[...]，表述清晰。”或“学生填写了[...]，部分内容无法识别。”）",
                "scoring_basis": (
                    """
【请针对【评分细则】中的【每一个填空项/答案要点】，清晰地解释你是如何判断该填空项/答案要点的是否得分以及得了多少分的。你需要：
1. 引用或概括学生在对应填空项/答案要点上的作答内容（如果未作答请说明）。
2. 结合评分细则，说明你的评分判断和理由。请用自然语言描述匹配过程。例如：'学生提到了[...]，匹配细则中的得分点[...]，得2分。
3. 明确指出该填空项/答案要点你最终给了多少分。
请确保你的解释能够清晰地支撑你在 `itemized_scores` 字段中给出的对应分数。
"""
                ),
                "itemized_scores": "【一个数字列表，例如 `[2, 0, 1]`。列表中的每个数字代表学生在【评分细则】中【对应顺序的每一个填空项/答案要点】上获得的【实际得分】。列表的长度应与评分细则中填空项/答案要点的数量一致。】"
              }
            }
          }
        }
        if ocr_mode:
            prompt_json['system_message'] = self._get_common_system_message(ocr_mode=True)
            prompt_json['user_task']['student_answer_image_placeholder'] = ""
        return json.dumps(prompt_json, ensure_ascii=False, indent=2)

    def _build_subjective_pointbased_prompt(self, standard_answer_rubric, ocr_mode=False):
        prompt_json = {
          "system_message": self._get_common_system_message(),
          "user_task": {
            "task_description": "请分析以下学生答案图片（按点给分主观题），并根据下方提供的评分细则给出评分。",
            "question_type_specific_instructions": "【题目类型：按点给分主观题】\n请仔细阅读【评分细则】中列出的【每一个得分点】及其对应的原始分值。\n你的核心任务是判断学生的答案内容是否清晰、准确地覆盖了这些【得分点】的要求。请严格按照细则中对每个【得分点】的描述和要求进行判断和给分。如果细则中包含“意思对即可”“酌情给分”或类似的灵活给分说明，请在评分依据中体现你的理解。",
            "scoring_rubric_placeholder": standard_answer_rubric,
            "student_answer_image_placeholder": "（图片内容会通过API的其他方式传入，这里仅为逻辑占位）",
            "student_answer_text_placeholder": "（OCR文本会通过API的其他方式传入，此字段仅为逻辑占位）",
            "output_format_specification": {
              "description": "请严格按照以下JSON格式返回结果，不要包含任何额外的解释性文字在JSON结构之外。确保输出的是有效的JSON，不要添加任何前缀、后缀、代码块标记或自然语言描述。直接输出JSON对象。",
              "format": {
                "student_answer_summary": "【请在此处对图片中的学生手写答案进行**中立、客观、不带任何主观判断**的【核心内容概括】（例如：“学生答案的主要内容为[...]，表述清晰。”或“学生填写了[...]，部分内容无法识别。”）",
                "scoring_basis": "【请针对【评分细则】中的【每一个得分点】，清晰地解释你是如何判断该得分点的是否得分以及得了多少分的。你需要：\n1. 引用或概括学生在对应得分点上的作答内容（如果未作答请说明）。\n2. 结合评分细则，说明你的评分判断和理由。\n3. 明确指出该得分点你最终给了多少分。\n请确保你的解释能够清晰地支撑你在 `itemized_scores` 字段中给出的对应分数。\n",
                "itemized_scores": "【一个数字列表，例如 `[3, 1, 0, 2]`。列表中的每个数字代表学生在【评分细则】中【对应顺序的每一个得分点】上获得的【实际得分】。列表的长度应与评分细则中得分点的数量一致。】"
              }
            }
          }
        }
        if ocr_mode:
            prompt_json['system_message'] = self._get_common_system_message(ocr_mode=True)
            prompt_json['user_task']['student_answer_image_placeholder'] = ""
        return json.dumps(prompt_json, ensure_ascii=False, indent=2)

    def _build_formula_proof_prompt(self, standard_answer_rubric, ocr_mode=False):
        prompt_json = {
          "system_message": self._get_common_system_message(),
          "user_task": {
            "task_description": "请分析以下学生答案图片（公式计算/证明题），并根据下方提供的评分细则给出评分。",
            "question_type_specific_instructions": "【题目类型：公式计算/证明题】\n请仔细阅读【评分细则】中对【解题的每一个关键步骤/采分点、所用公式的准确性、计算结果的正确性、证明逻辑的严密性以及数学/物理/化学符号和书写的规范性】的具体要求和分值分配。\n你的核心任务是逐一核对学生的解题过程和最终答案是否符合细则中每一个【关键步骤/采分点】的标准。",
            "scoring_rubric_placeholder": standard_answer_rubric,
            "student_answer_image_placeholder": "（图片内容会通过API的其他方式传入，这里仅为逻辑占位）",
            "student_answer_text_placeholder": "（OCR文本会通过API的其他方式传入，此字段仅为逻辑占位）",
            "output_format_specification": {
              "description": "请严格按照以下JSON格式返回结果，不要包含任何额外的解释性文字在JSON结构之外。确保输出的是有效的JSON，不要添加任何前缀、后缀、代码块标记或自然语言描述。直接输出JSON对象。",
              "format": {
                "student_answer_summary": "【请在此处对图片中的学生手写答案进行**中立、客观、不带任何主观判断**的【核心内容概括】（例如：“学生答案的主要内容为[...]，表述清晰。”或“学生填写了[...]，部分内容无法识别。”）",
                "scoring_basis": "【请针对【评分细则】中的【每一个关键步骤/采分点】，清晰地解释你是如何判断该步骤/采分点的是否得分以及得了多少分的。你需要：\n1. 引用或概括学生在对应步骤/采分点上的解题过程或书写内容（如果未作答或跳过请说明）。\n2. 结合评分细则，说明你的评分判断和理由（例如：公式是否正确，代入是否无误，计算结果是否准确，证明逻辑是否严密等）。\n3. 明确指出该步骤/采分点你最终给了多少分。\n请确保你的解释能够清晰地支撑你在 `itemized_scores` 字段中给出的对应分数。\n",
                "itemized_scores": "【一个数字列表，例如 `[2, 2, 0, 1]`。列表中的每个数字代表学生在【评分细则】中【对应顺序的每一个关键步骤/采分点】上获得的【实际得分】。列表的长度应与评分细则中关键步骤/采分点的数量一致。】"
              }
            }
          }
        }
        if ocr_mode:
            prompt_json['system_message'] = self._get_common_system_message(ocr_mode=True)
            prompt_json['user_task']['student_answer_image_placeholder'] = ""
        return json.dumps(prompt_json, ensure_ascii=False, indent=2)

    def _build_holistic_evaluation_prompt(self, standard_answer_rubric, ocr_mode=False):
        prompt_json = {
          "system_message": self._get_common_system_message(),
          "user_task": {
            "task_description": "请分析以下学生答案图片（整体评估开放题，如作文、论述等），并根据下方提供的评分细则给出评分。",
            "question_type_specific_instructions": "【题目类型：整体评估开放题】\n请仔细阅读【评分细则】中关于【各项评估维度/评分标准或等级描述】（例如：内容是否切题、思想深度、结构逻辑性、语言表达的准确性与文采、观点创新性、书写规范等）。\n你的核心任务是基于这些宏观标准，对学生的答案进行全面的、综合的判断，并给出一个最终总分。请在评分依据中清晰阐述你是如何结合细则中的各个评估维度得出该总分的。",
            "scoring_rubric_placeholder": standard_answer_rubric,
            "student_answer_image_placeholder": "（图片内容会通过API的其他方式传入，这里仅为逻辑占位）",
            "student_answer_text_placeholder": "（OCR文本会通过API的其他方式传入，此字段仅为逻辑占位）",
            "output_format_specification": {
              "description": "请严格按照以下JSON格式返回结果，不要包含任何额外的解释性文字在JSON结构之外。确保输出的是有效的JSON，不要添加任何前缀、后缀、代码块标记或自然语言描述。直接输出JSON对象。",
              "format": {
                "student_answer_summary": "【请在此处对图片中的学生手写答案进行**中立、客观、不带任何主观判断**的【核心内容概括】（例如：“学生答案的主要内容为[...]，表述清晰。”或“学生填写了[...]，部分内容无法识别。”）",
                "scoring_basis": "【请在此处综合阐述你给出 `itemized_scores` 中最终总分的详细理由。你需要：\n1. 参照【评分细则】中列出的各项整体评估维度（例如：内容切题性、思想深度、结构逻辑、语言表达、书写规范等）。\n2. 针对每一个主要评估维度，清晰描述从图片中观察到的学生表现。\n3. 解释这些不同维度的表现是如何共同作用，最终形成了你在 `itemized_scores` 中给出的那个总分。\n请确保你的阐述逻辑清晰、依据充分，并直接关联到最终的评分结果。\n（例如：\n- 维度1（如内容切题性）：学生表现[...具体描述...]，符合/不符合细则的[...某标准...]。\n- 维度2（如结构逻辑）：学生表现[...具体描述...]，符合/不符合细则的[...某标准...]。\n- (依此类推所有主要维度)\n- 综合评价：基于以上各维度表现，[简述如何综合考虑，例如哪些是主要影响因素]，并对照评分细则中的等级描述，最终评定总分为XX分。）",
                "itemized_scores": "【一个【只包含一个数字的列表】，例如 `[45]` 或 `[8]`。这个数字代表你根据【评分细则】中的整体评估标准/评分维度给出的【最终总分】。】"
              }
            }
          }
        }
        if ocr_mode:
            prompt_json['system_message'] = self._get_common_system_message(ocr_mode=True)
            prompt_json['user_task']['student_answer_image_placeholder'] = ""
        return json.dumps(prompt_json, ensure_ascii=False, indent=2)

    def select_and_build_prompt(self, standard_answer, question_type, ocr_mode=False):
        """
        根据题目类型选择并构建相应的Prompt。
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
            error_msg = "评分细则为空或仅包含空白，阅卷已暂停，请检查配置并手动处理当前题目。"
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
        if not student_answer_summary or not scoring_basis:
            return False

        # 检查是否包含请求图片内容的关键词
        request_keywords = [
            "请提供学生答案图片内容",
            "请提供学生答案图片",
            "需要学生答案图片",
            "无法获取图片内容",
            "图片内容不可用"
        ]

        summary_lower = student_answer_summary.lower()
        basis_lower = scoring_basis.lower()

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

    def _set_error_state(self, reason):
        """统一设置错误状态（线程安全）"""
        with self._state_lock:
            self.completion_status = "error"
            self.interrupt_reason = reason
            self.running = False
        self.log_signal.emit(f"错误: {reason}", True, "ERROR")

    def run(self):
        """线程主函数，执行自动阅卷流程
        
        P0修复：改进异常处理
        - 使用分类异常捕获（ValueError、requests.RequestException等）而非宽泛的Exception
        - 添加finally块确保资源清理
        - 确保错误时正确清理状态和临时资源
        """
        # 重置状态
        self.completion_status = "running"
        self.completed_count = 0
        self.total_question_count_in_run = 0
        self.interrupt_reason = ""
        self.running = True
        self.log_signal.emit("自动阅卷线程已启动", False, "INFO")

        # Provide safe defaults for variables that may be referenced in finally() blocks
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
            ocr_mode = params.get('ocr_mode', '') if isinstance(params, dict) else ''
            self.log_signal.emit(f"OCR 模式: {ocr_mode}", False, "DETAIL")

            if not question_configs:
                self._set_error_state("未配置题目信息")
                return

            # 设置总题数（在单题模式下总是1）
            self.total_question_count_in_run = len(question_configs)

            # 单题模式：只处理第一题
            # 记录开始时间
            start_time = time.time()
            elapsed_time = 0

            # 执行循环
            for i in range(cycle_number):
                if not self.running:
                    break

                self.log_signal.emit(f"开始第 {i+1}/{cycle_number} 次阅卷", False, "DETAIL")

                # 单题模式只处理第一题
                q_config = question_configs[0]  # 第一题配置
                question_index = 1
                self.log_signal.emit(f"正在处理第 {question_index} 题", False, "DETAIL")

                # 设置当前题目索引
                self.api_service.set_current_question(question_index)

                # 获取题目配置
                score_input_pos = q_config.get('score_input_pos', (0, 0))
                confirm_button_pos = q_config.get('confirm_button_pos', (0, 0))
                standard_answer = q_config.get('standard_answer', '')

                # 检查位置配置
                if score_input_pos == (0, 0) or confirm_button_pos == (0, 0):
                    self._set_error_state(f"第 {question_index} 题未配置位置信息")
                    break

                # 获取当前题目的答案区域
                answer_area_data = q_config.get('answer_area', {})
                if not answer_area_data or not all(key in answer_area_data for key in ['x1', 'y1', 'x2', 'y2']):
                    self._set_error_state(f"第 {question_index} 题未配置答案区域")
                    break

                # 获取题目类型
                question_type = q_config.get('question_type', 'Subjective_PointBased_QA')
                if not question_type:
                    self.log_signal.emit(f"警告：第 {question_index} 题未配置题目类型，将使用默认类型 'Subjective_PointBased_QA'。", True, "ERROR")
                    question_type = 'Subjective_PointBased_QA'

                # 截取答案区域
                x1 = answer_area_data.get('x1', 0)
                y1 = answer_area_data.get('y1', 0)
                x2 = answer_area_data.get('x2', 0)
                y2 = answer_area_data.get('y2', 0)

                # 确保 x1, y1 是左上角坐标
                x = min(x1, x2)
                y = min(y1, y2)
                width = abs(x2 - x1)
                height = abs(y2 - y1)

                answer_area_tuple = (x, y, width, height)

                img_str = self.capture_answer_area(answer_area_tuple)
                if not self.running: break  # 如果截取失败，整个流程已停止

                # 构建JSON Prompt
                self.log_signal.emit(f"为第 {question_index} 题 (类型: {question_type}) 构建Prompt...", False, "DETAIL")
                # 根据参数自动切换为 OCR 模式提示词（当 ocr_mode == 'baidu_ocr' 时）
                text_prompt_for_api = self.select_and_build_prompt(standard_answer, question_type, ocr_mode=(ocr_mode == 'baidu_ocr'))

                if text_prompt_for_api is None:
                    if not self.running: break
                    continue

                # 检查是否启用OCR辅助识别
                ocr_text = ""
                ocr_meta = None
                if hasattr(self, 'parameters') and self.parameters.get('ocr_mode') == 'baidu_ocr':
                    ocr_text, ocr_meta = self._perform_ocr_recognition(img_str, question_type)
                    # 在UI中显示OCR识别结果
                    if ocr_text and isinstance(ocr_text, str) and ocr_text.strip():
                        self.log_signal.emit(f"OCR识别结果: {ocr_text[:200]}", False, "RESULT")
                    else:
                        self.log_signal.emit("OCR未能识别到文字", False, "RESULT")

                    # 如果检测到需要人工介入，停止并等待人工处理
                    if ocr_meta and ocr_meta.get('manual_intervention'):
                        reason = ocr_meta.get('reason') or 'OCR质量不达标，需人工介入'
                        self.log_signal.emit(f"OCR质量不足，暂停阅卷并等待人工复核: {reason}", True, "ERROR")
                        try:
                            # 发送人工介入信号到UI
                            self.manual_intervention_signal.emit(reason, ocr_text)
                        except Exception:
                            pass
                        self._set_error_state(f"OCR质量不足，人工复核: {reason}")
                        break
                    # 额外保护：若未返回meta或识别文本为空（即使没有meta标记为人工），也应暂停并等待人工处理
                    if (ocr_meta is None) or (not ocr_text or not ocr_text.strip()):
                        reason = 'OCR未能识别到有效文本或未返回OCR元信息，需人工介入'
                        self.log_signal.emit(f"OCR识别文本为空或元信息缺失，暂停阅卷并等待人工复核: {reason}", True, "ERROR")
                        self._set_error_state(reason)
                        break

                # 调用API进行评分
                img_for_api = img_str
                if hasattr(self, 'parameters') and self.parameters.get('ocr_mode') == 'baidu_ocr':
                    # OCR模式：不再上传原图给AI，仅发送OCR文本
                    img_for_api = ""

                eval_result = self.evaluate_answer(
                    img_for_api, text_prompt_for_api, q_config, dual_evaluation, score_diff_threshold, ocr_text
                )

                # 检查是否完全失败，如果失败则完全停止阅卷
                if eval_result is None:
                    self.log_signal.emit("评分处理完全失败，阅卷停止，等待用户手动操作", True, "ERROR")
                    self._set_error_state("评分处理失败，需手动处理")
                    break

                score, reasoning_data, itemized_scores_data, confidence_data, raw_ai_response = eval_result

                # 如果评分处理失败，完全停止阅卷，等待用户手动操作
                if score is None:
                    self.log_signal.emit(f"第 {question_index} 题评分失败，阅卷完全停止，等待用户手动操作", True, "ERROR")
                    self._set_error_state(f"第 {question_index} 题评分失败，需手动处理")
                    break

                # 输入分数
                self.input_score(score, score_input_pos, confirm_button_pos, q_config)

                if not self.running:
                    break

                # 更新进度
                self.completed_count = i + 1
                total = cycle_number
                self.progress_signal.emit(self.completed_count, total)

                # 记录阅卷结果：将 OCR 元数据一并保存（如果有）
                self.record_grading_result(question_index, score, img_str, reasoning_data, itemized_scores_data, confidence_data, raw_ai_response, ocr_text, ocr_meta)

                # 等待指定时间
                if self.running and wait_time > 0:
                    time.sleep(wait_time)

                # 单题模式不需要翻页逻辑

            # 计算总用时
            elapsed_time = time.time() - start_time
            if self.running:
                self.log_signal.emit(f"自动阅卷完成，总用时: {elapsed_time:.2f} 秒", False, "INFO")
                self.completion_status = "completed"
            else:
                if self.completion_status == "running":
                    self.completion_status = "error"
                    self.interrupt_reason = "未知错误导致中断"

        except ValueError as e:
            # P0修复：分类异常捕获 - 值错误（配置、参数等）
            error_detail = traceback.format_exc()
            error_msg = f"配置或参数错误: {str(e)}"
            self.log_signal.emit(f"{error_msg}\n{error_detail}", True, "ERROR")
            self.completion_status = "error"
            self.interrupt_reason = error_msg
        
        except KeyError as e:
            # P0修复：分类异常捕获 - 键错误（配置字段缺失等）
            error_detail = traceback.format_exc()
            error_msg = f"配置字段缺失: {str(e)}"
            self.log_signal.emit(f"{error_msg}\n{error_detail}", True, "ERROR")
            self.completion_status = "error"
            self.interrupt_reason = error_msg
        
        except Exception as e:
            # P0修复：分类异常捕获 - 通用异常（但更具体的异常会优先捕获）
            error_detail = traceback.format_exc()
            self.log_signal.emit(f"自动阅卷出错: {str(e)}\n{error_detail}", True, "ERROR")
            self.completion_status = "error"
            self.interrupt_reason = f"系统错误: {str(e)}"

        finally:
            # P0修复：增强的异常恢复机制和资源清理
            # 确保无论发生什么都能正确清理和通知UI
            self.running = False
            
            # P0修复：清理所有临时资源
            try:
                self._cleanup_resources()
            except Exception as cleanup_error:
                try:
                    self.log_signal.emit(f"资源清理失败: {str(cleanup_error)}", False, "WARNING")
                except:
                    pass
            
            # 第一步：生成汇总记录（即使失败也继续执行后续步骤）
            try:
                self.generate_summary_record(cycle_number, dual_evaluation, score_diff_threshold, elapsed_time)
            except Exception as summary_error:
                try:
                    self.log_signal.emit(f"生成汇总记录失败: {str(summary_error)}", True, "ERROR")
                except Exception:
                    # 如果连日志信号都失败了，至少打印到控制台
                    print(f"[严重错误] 生成汇总记录失败且无法发送日志: {summary_error}")

            # 第二步：确保UI收到完成信号（使用多重保护）
            reason = "未知错误"  # 初始化reason变量
            try:
                if self.completion_status == "completed":
                    try:
                        self.finished_signal.emit()
                    except Exception as e:
                        print(f"[严重错误] 发送finished_signal失败: {e}")
                elif self.completion_status == "threshold_exceeded":
                    try:
                        reason = self.interrupt_reason or "双评分差超过阈值"
                        self.threshold_exceeded_signal.emit(reason)
                    except Exception as e:
                        print(f"[严重错误] 发送threshold_exceeded_signal失败: {e}")
                        # 尝试降级为error_signal
                        try:
                            self.error_signal.emit(reason)
                        except Exception:
                            pass
                else:
                    try:
                        reason = self.interrupt_reason or "未知错误"
                        self.error_signal.emit(reason)
                    except Exception as e:
                        print(f"[严重错误] 发送error_signal失败: {e}")
            except Exception as final_error:
                # 最后的防线：即使信号发送失败，也要记录
                print(f"[致命错误] finally块中发送信号时出现异常: {final_error}")
                # 尝试通过日志信号通知（如果还可用）
                try:
                    self.log_signal.emit(
                        f"阅卷线程终止时发生致命错误，状态={self.completion_status}: {final_error}",
                        True, "ERROR"
                    )
                except Exception:
                    pass  # 真的无能为力了

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
        """截取答案区域，带重试机制

        Args:
            area: 答案区域坐标 (x, y, width, height)

        Returns:
            base64编码的图片字符串，失败时直接停止整个流程
        """
        max_retries = 3
        x, y, width, height = area

        # 确保宽度和高度为正值
        if width < 0:
            x = x + width
            width = abs(width)
        if height < 0:
            y = y + height
            height = abs(height)

        for attempt in range(max_retries):
            screenshot = None
            try:
                self.log_signal.emit(f"正在截取答案区域 (坐标: {x},{y}, 尺寸: {width}x{height}) - 尝试 {attempt + 1}/{max_retries}", False, "DETAIL")

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
                error_msg = f"截取答案区域失败 (尝试 {attempt + 1}/{max_retries}): {str(e)}"
                self.log_signal.emit(error_msg, True, "ERROR")
                
                # 异常处理中也要确保清理资源
                if screenshot:
                    try:
                        screenshot.close()
                    except:
                        pass
                    screenshot = None

                if attempt < max_retries - 1:
                    # 不是最后一次尝试，等待后重试
                    time.sleep(1)
                    continue
                else:
                    # 最后一次尝试也失败了，直接停止整个流程
                    final_error = f"截取答案区域失败，已重试{max_retries}次。坐标: ({x},{y}), 尺寸: {width}x{height}。错误: {str(e)}"
                    self._set_error_state(final_error)
                    return None  # 虽然不会被使用，但保持接口一致性

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
                img = Image.open(bytes_io)

                to_gray = bool(getattr(self.api_service.config_manager, 'ocr_preprocess_to_gray', True))
                max_width = int(getattr(self.api_service.config_manager, 'ocr_preprocess_max_width', 1200))
                jpeg_quality = int(getattr(self.api_service.config_manager, 'ocr_preprocess_jpeg_quality', 85))

                if to_gray:
                    img = img.convert('L')
                else:
                    img = img.convert('RGB')

                w, h = img.size
                if w > max_width:
                    new_h = int(h * (max_width / w))
                    try:
                        resample = Image.Resampling.LANCZOS
                        img = img.resize((max_width, new_h), resample)
                    except Exception:
                        # Pillow older versions may not have Resampling.LANCZOS; fallback to default resize
                        img = img.resize((max_width, new_h))

                buffered = BytesIO()
                img.save(buffered, format='JPEG', quality=jpeg_quality)
                b64_out = base64.b64encode(buffered.getvalue()).decode()
                return f"data:image/jpeg;base64,{b64_out}"
            finally:
                # 确保Image对象被释放
                if img:
                    img.close()
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


    def evaluate_answer(self, img_str, prompt, current_question_config, dual_evaluation=False, score_diff_threshold=10, ocr_text=""):
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

        # 调用第一个API并处理结果
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
            return None, error1, None, None, None

        # 如果不启用双评，直接返回第一个API的结果
        if not dual_evaluation:
            return score1, reasoning1, scores1, confidence1, response_text1

        # 如果启用双评，继续调用第二个API
        score2, reasoning2, scores2, confidence2, response_text2, error2 = self._call_and_process_single_api(
            self.api_service.call_second_api,
            img_str,
            prompt,
            current_question_config,
            api_name="第二个API",
            ocr_text=ocr_text
        )
        if error2:
            self._set_error_state(error2)
            return None, error2, None, None, None

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
            return None, error_dual, None, None, None

        return final_score, combined_reasoning, combined_scores, combined_confidence, None

    def _perform_ocr_recognition(self, img_str, question_type='Subjective_PointBased_QA'):
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
            
            # 调用百度DocAnalysis接口，获取结构化结果
            data, error = self.api_service.call_baidu_doc_analysis_structured(img_str)
            if error:
                self.log_signal.emit(f"OCR识别失败: {error}", True, "WARNING")
                return "", {'manual_intervention': True, 'reason': f'OCR接口错误: {error}'}

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
                        'detailed_reason': f"⚠️ OCR返回的数据格式异常，无法信任\n\n错误详情：\n{error_msg}\n\n建议：请检查百度OCR的返回格式是否符合预期，或联系技术支持"
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
                    'detailed_reason': f"⚠️ OCR置信度计算异常，无法信任识别结果\n\n错误详情：\n{error_msg}\n\n建议：请手动检查答题卡清晰度，或联系技术支持"
                }
            
            # 🎯 获取用户选择的质量等级和对应阈值
            quality_level = getattr(self.api_service.config_manager, 'ocr_quality_level', 'moderate')
            # 转换UI文本到内部值
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
                'detailed_reason': f"⚠️ OCR处理过程发生异常\n\n错误详情：\n{str(e)}\n\n建议：请联系技术支持或手动评分"
            }


    def _call_and_process_single_api(self, api_call_func, img_str, prompt, q_config, api_name="API", max_retries=3, ocr_text=""):
        """
        调用指定的API函数，并处理其响应。支持重试机制以提高稳定性，包括JSON解析失败的重试。

        Args:
            api_call_func: 要调用的API服务方法 (e.g., self.api_service.call_first_api)
            img_str: 图片base64字符串
            prompt: 提示词
            q_config: 当前题目配置
            api_name: 用于日志的API名称
            max_retries: 最大重试次数，默认3次
            ocr_text: OCR识别的文本，用于辅助评分

        Returns:
            一个元组 (score, reasoning, itemized_scores, confidence, error_message)
        """
        for attempt in range(max_retries):
            if attempt > 0:
                self.log_signal.emit(f"{api_name}第{attempt}次重试...", False, "DETAIL")
                time.sleep(1)  # 短暂延迟，避免过于频繁的请求

            self.log_signal.emit(f"正在调用{api_name}进行评分... (尝试 {attempt + 1}/{max_retries})", False, "DETAIL")
            # 如果有OCR文本，打印一条简短日志，便于调试
            if ocr_text and isinstance(ocr_text, str) and ocr_text.strip():
                snippet = ocr_text.replace('\n', ' ')[:80]
                self.log_signal.emit(f"传入OCR文本到{api_name}（前80字符）: {snippet}", False, "DETAIL")
            response_text, error_from_call = api_call_func(img_str, prompt, ocr_text)

            if error_from_call or not response_text:
                error_msg = f"{api_name}调用失败或响应为空: {error_from_call}"
                if attempt == max_retries - 1:  # 最后一次尝试失败
                    self.log_signal.emit(error_msg, True, "ERROR")
                    return None, None, None, None, response_text, error_msg
                else:
                    self.log_signal.emit(f"{error_msg}，准备重试...", True, "ERROR")
                    continue

            success, result_data = self.process_api_response((response_text, None), q_config)

            if success:
                score, reasoning, itemized_scores, confidence = result_data
                return score, reasoning, itemized_scores, confidence, response_text, None
            else:
                error_info = result_data
                # 检查是否为JSON解析错误（支持旧tuple格式和新的显式dict格式），如果是则重试API调用
                is_json_parse_error = (
                    (isinstance(error_info, tuple) and len(error_info) >= 2 and error_info[0] == "json_parse_error") or
                    (isinstance(error_info, dict) and error_info.get('parse_error') and error_info.get('error_type') == 'json_parse_error')
                )

                # 检查是否为人工介入信号，若是则不重试，立即返回错误以便上层停止处理
                is_manual_intervention = (
                    isinstance(error_info, dict) and error_info.get('manual_intervention')
                )
                if is_manual_intervention:
                    # 安全地读取字段
                    error_msg = error_info.get('message') if isinstance(error_info, dict) else str(error_info)
                    raw_fb = error_info.get('raw_feedback') if isinstance(error_info, dict) else ''
                    self.log_signal.emit(f"{api_name}检测到人工介入请求: {error_msg}", True, "ERROR")
                    return None, None, None, None, response_text, error_msg

                if is_json_parse_error:
                    # 兼容tuple和dict两种格式以获得错误信息与原始响应
                    if isinstance(error_info, tuple):
                        error_msg = error_info[1] if len(error_info) > 1 else str(error_info)
                        raw_response = error_info[2] if len(error_info) > 2 else response_text
                    else:
                        error_msg = error_info.get('message', str(error_info))
                        raw_response = error_info.get('raw_response', response_text)

                    if attempt == max_retries - 1:  # 最后一次尝试的JSON解析失败
                        final_error_msg = f"{api_name}JSON解析失败（已重试{max_retries}次）。错误: {error_msg}"
                        self.log_signal.emit(final_error_msg, True, "ERROR")
                        return None, None, None, None, raw_response, final_error_msg
                    else:
                        self.log_signal.emit(f"{api_name}JSON解析失败: {error_msg}，重新调用API...", True, "ERROR")
                        continue  # 重试API调用
                else:
                    # 其他类型的处理失败
                    if attempt == max_retries - 1:  # 最后一次尝试的处理失败
                        error_msg = f"{api_name}评分处理失败（已重试{max_retries}次）。错误: {error_info}"
                        self.log_signal.emit(error_msg, True, "ERROR")
                        return None, None, None, None, response_text, error_msg
                    else:
                        self.log_signal.emit(f"{api_name}处理失败: {error_info}，准备重试...", True, "ERROR")
                        continue

        # 理论上不会到达这里，但为了安全，返回一致的6元组 (score, reasoning, itemized_scores, confidence, response_text, error_msg)
        return None, None, None, None, None, f"{api_name}重试后仍失败"

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

    def process_api_response(self, response, current_question_config):
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

            # 验证必需字段是否存在
            required_fields = ["student_answer_summary", "scoring_basis", "itemized_scores"]
            missing_fields = [field for field in required_fields if field not in data]
            if missing_fields:
                error_msg = f"API响应JSON缺少必需字段: {', '.join(missing_fields)}"
                self.log_signal.emit(error_msg, True, "ERROR")
                return False, error_msg

            student_answer_summary = data.get("student_answer_summary", "未能提取学生答案摘要")
            scoring_basis = data.get("scoring_basis", "未能提取评分依据")
            itemized_scores_from_json = data.get("itemized_scores")
            confidence_data = {}  # 置信度功能暂时停用

            self.log_signal.emit(f"AI提取的学生答案摘要: {student_answer_summary}", False, "RESULT")
            self.log_signal.emit(f"AI评分依据: {scoring_basis}", False, "RESULT")

            # 检查是否为无法识别的情况，如果是则停止阅卷
            if self._is_unrecognizable_answer(student_answer_summary, itemized_scores_from_json):
                error_msg = f"学生答案图片无法识别，停止阅卷。请检查图片质量或手动处理。AI反馈: {student_answer_summary}"
                self.log_signal.emit(error_msg, True, "ERROR")
                return False, error_msg

            # 检查AI是否明确请求人工介入（OCR相关的停止信号）
            manual_msg = self._detect_manual_intervention_feedback(student_answer_summary, scoring_basis)
            if manual_msg:
                error_msg = f"检测到AI请求人工介入: {manual_msg}"
                self.log_signal.emit(error_msg, True, "ERROR")
                try:
                    self.manual_intervention_signal.emit(error_msg, student_answer_summary)
                except Exception:
                    pass
                # 返回带有标记的结构，便于上层立即停止且不重试
                return False, {'manual_intervention': True, 'message': error_msg, 'raw_feedback': student_answer_summary}

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
                    numeric_scores_list_for_return = [sanitize_score(s) for s in itemized_scores_from_json]
                    calculated_total_score = sum(numeric_scores_list_for_return)
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
        """
        try:
            q_min_score = float(current_question_config.get('min_score', self.min_score))
            # 从题目配置中获取该题的满分
            q_max_score = float(current_question_config.get('max_score', self.max_score))

            if not isinstance(total_score_from_json, (int, float)):
                error_msg = f"API返回的计算总分 '{total_score_from_json}' 不是有效数值。"
                self.log_signal.emit(error_msg, True, "ERROR")
                self._set_error_state(error_msg)
                return None

            final_score = float(total_score_from_json)

            # 范围校验与满分截断
            if final_score < q_min_score:
                self.log_signal.emit(f"计算总分 {final_score} 低于最低分 {q_min_score}，修正为 {q_min_score}。", True, "ERROR")
                final_score = q_min_score
            elif final_score > q_max_score:
                self.log_signal.emit(f"计算总分 {final_score} 超出题目满分 {q_max_score} (原始AI总分: {total_score_from_json})，将修正为满分 {q_max_score}。", True, "ERROR")
                final_score = q_max_score # 修正为满分

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
        if not student_answer_summary and not scoring_basis:
            return None

        combined = " ".join([str(student_answer_summary or ""), str(scoring_basis or "")]).lower()

        # 常见的人工介入提示关键词/短语（含中/英常见表述）
        triggers = [
            '需人工介入', '人工介入', '需要人工介入', '需人工复核', '人工复核',
            '无法判定', '无法评判', '无法判断', '无法评分',
            '无法判定的涂改', '涂改', '自我修正', '自我更正', '改错',
            '噪声太大', '识别噪声', '识别错误', '识别失败', '乱码',
            '逻辑混乱', '算式不成立', '逻辑不通顺', 'ocr 文本逻辑混乱', '疑似ocr错误',
            'manual intervention', 'need manual', 'cannot judge', 'cannot score', 'unclear', 'requires manual'
        ]

        for t in triggers:
            if t in combined:
                return t

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

            # 1. 获取用户配置的分数步长并处理到最近的步长倍数
            score_step = getattr(self.api_service.config_manager, 'score_rounding_step', 0.5)
            final_score_processed = round_to_step(final_score_to_input, score_step)
            
            # 获取当前题目的最小分值，用于最终修正 (q_max_score 已在上方获取并更新为使用 self.max_score 作为默认值)
            q_min_score = float(current_question_config.get('min_score', self.min_score)) 

            self.log_signal.emit(f"AI得分 (原始范围 [{q_min_score}-{q_max_score}]): {final_score_to_input}, 步长{score_step}四舍五入后: {final_score_processed}", False, "INFO")

            # 2. 修正四舍五入后的分数，确保其严格在 [q_min_score, q_max_score] 范围内
            #    final_score_to_input 已经由 _validate_and_finalize_score 保证在原始 [min_score, max_score] 内。
            #    这里的 final_score_processed 是 round_to_nearest_half(final_score_to_input) 的结果。

            if final_score_processed < q_min_score:
                self.log_signal.emit(f"四舍五入到0.5倍数后得分 ({final_score_processed}) 低于题目最低分 ({q_min_score})，将修正为最低分: {q_min_score}。", True, "ERROR")
                final_score_processed = q_min_score
            elif final_score_processed > q_max_score: # 使用 elif
                self.log_signal.emit(f"四舍五入到0.5倍数后得分 ({final_score_processed}) 高于题目满分 ({q_max_score})，将修正为满分: {q_max_score}。", True, "ERROR")
                final_score_processed = q_max_score

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
                self.log_signal.emit(f"三步拆分结果: s1={s1}, s2={s2}, s3={s3} (总和: {round_to_nearest_half(s1+s2+s3)})", False, "INFO")

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

            if is_dual:
                # 双评模式
                record.update({
                    'api1_student_answer_summary': reasoning_data.get('api1_summary', 'AI未提供'),
                    'api1_scoring_basis': reasoning_data.get('api1_basis', 'AI未提供'),
                    'api1_raw_score': reasoning_data.get('api1_raw_score', 0.0),
                    'api1_raw_response': reasoning_data.get('api1_raw_response', 'AI未提供'),
                    'api2_student_answer_summary': reasoning_data.get('api2_summary', 'AI未提供'),
                    'api2_scoring_basis': reasoning_data.get('api2_basis', 'AI未提供'),
                    'api2_raw_score': reasoning_data.get('api2_raw_score', 0.0),
                    'api2_raw_response': reasoning_data.get('api2_raw_response', 'AI未提供'),
                    'score_difference': reasoning_data.get('score_difference', 0.0),
                    'score_diff_threshold': self.parameters.get('score_diff_threshold', "AI未提供"),
                    'ocr_recognized_text': ocr_text if ocr_text else "未启用OCR或识别失败",
                    'ocr_confidence_meta': ocr_meta if ocr_meta else {},
                })
                if isinstance(itemized_scores_data, dict):
                    record['api1_itemized_scores'] = itemized_scores_data.get('api1_scores', [])
                    record['api2_itemized_scores'] = itemized_scores_data.get('api2_scores', [])
                # 此版本暂时不启用置信度功能，今后如果需要再启用
                # if isinstance(confidence_data, dict):
                #     api1_conf = confidence_data.get('api1_confidence', {})
                #     if isinstance(api1_conf, dict):
                #         record['api1_confidence_score'] = api1_conf.get('score')
                #         record['api1_confidence_reason'] = api1_conf.get('reason', 'AI未提供')

                #     api2_conf = confidence_data.get('api2_confidence', {})
                #     if isinstance(api2_conf, dict):
                #         record['api2_confidence_score'] = api2_conf.get('score')
                #         record['api2_confidence_reason'] = api2_conf.get('reason', 'AI未提供')

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
                record.update({
                    'student_answer': summary,
                    'reasoning_basis': basis,
                    'sub_scores': str(itemized_scores_data) if itemized_scores_data is not None else "AI未提供",
                    'raw_ai_response': raw_ai_response if raw_ai_response is not None else "AI未提供",
                    'ocr_recognized_text': ocr_text if ocr_text else "未启用OCR或识别失败",
                    'ocr_confidence_meta': ocr_meta if ocr_meta else {},
                })
                    # 此版本暂时不启用置信度功能，今后如果需要再启用
                    # if isinstance(confidence_data, dict):
                    #     record['confidence_score'] = confidence_data.get('score')
                    #     record['confidence_reason'] = confidence_data.get('reason', 'AI未提供')

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
