import os
import time
import base64
import traceback
import pyautogui
import datetime
from io import BytesIO
from PIL import ImageGrab
from PyQt5.QtCore import QThread, pyqtSignal
import math
import json


# 函数：将数值四舍五入到最接近的0.5的倍数
def round_to_nearest_half(value: float) -> float:
    """
    将数值四舍五入到最接近的0.5的倍数。
    例如: 7.2 -> 7.0, 7.25 -> 7.5, 7.6 -> 7.5, 7.75 -> 8.0
    """
    return math.floor(value * 2 + 0.5) / 2.0


class AutoThread(QThread):
    """自动阅卷线程，负责执行自动阅卷流程"""

    # 定义信号
    # update_signal = pyqtSignal(str)  # 更新建议文本
    log_signal = pyqtSignal(str, bool, str)  # 日志信息，带错误标志和级别
    progress_signal = pyqtSignal(int, int)  # 进度信息，当前进度和总进度
    record_signal = pyqtSignal(dict)  # 记录信号，发送阅卷记录
    error_signal = pyqtSignal(str)  # 错误信号
    finished_signal = pyqtSignal()  # 线程完成信号
    threshold_exceeded_signal = pyqtSignal(str)  # 双评分差超过阈值信号

    def __init__(self, api_service):
        super().__init__()
        self.api_service = api_service
        self.running = False
        self.parameters = {}
        self.max_score = 100  # 默认最高分 (这个是线程级别的默认，会被题目配置覆盖)
        self.min_score = 0    # 默认最低分 (同上)
        self.question_configs = [] # 这个应该在 set_parameters 中从参数获取，这里初始化为空列表是OK的
        self.completion_status = "running"
        self.completed_count = 0
        self.interrupt_reason = ""

        # API配置信息存储
        self.first_model_id = ""
        self.second_model_id = ""

        # 单题模式固定为True
        self.is_single_question_one_run = True
        self.total_question_count_in_run = 1 # 单题模式固定为1题

    # --- 新增的Prompt构建方法 ---
    def _get_common_system_message(self):
        subject = "通用"  # 默认科目设置为 "通用"

        if hasattr(self.api_service, 'config_manager') and self.api_service.config_manager:
            # 尝试从配置中获取科目，如果不存在，subject_from_config 会是 None
            subject_from_config = getattr(self.api_service.config_manager, 'subject', None)

            if subject_from_config and subject_from_config.strip():
                # 如果配置中的科目有效（非空、非纯空格），则使用配置中的科目
                subject = subject_from_config
            # 否则 (配置为空、纯空格、或不存在科目配置), subject 保持为 "通用"
        # 如果 config_manager 本身不存在, subject 也保持为 "通用"

        return (
            f"你是一位经验丰富、严谨细致的【{subject}】资深阅卷老师。"
            "你的核心任务是：根据用户提供的【评分细则】和【题目类型说明】，对学生答案的图片内容进行深入分析和准确评分。"
            "请你务必严格按照给定的JSON格式输出分析结果。\n\n"
            "在整个评分过程中，请严格遵守以下【评分总则】：\n"
            "1.  【关于涂改】：学生在答案文字上所作的任何横线、斜线、删除线或类似标记，均视为学生主动删除的内容。此部分内容不参与评分，即：既不因其正确而给分，也不因其可能存在的错误而不给分。请注意：学生可能会在原有答案涂改后，将新的答案写在涂改区域的旁边、上方或下方。如果这些补充内容清晰可辨且与题目相关，应将其视为学生最终意图表达的一部分，并纳入评分范围。\n"
            "2.  【严格依据细则】：你的所有评分判断【必须且仅能严格依据】用户在【评分细则】中明确列出的每一个【得分点/答案要点/关键步骤/评估维度】（具体称呼依据题目类型而定）的标准和给分说明。严禁对评分细则进行任何形式的补充、推测、联想或超出细则范围进行给分或不给分。\n"
            "3.  【仅限图像内容】：你的评分判断【仅能依据】从学生答题卡图片中真实可辨识的手写内容。严禁根据图片以外的任何信息（包括你对该学科知识的掌握、对评分细则的记忆或普遍常识）来猜测、臆断或虚构学生可能想表达的答案。\n"
            "4.  【关于扣分】：评分主要依据学生达到得分点的程度给分。**然而，如果【评分细则】中明确包含“扣X分”的指令（例如：'每处过度解读扣0.5分'，'关键词误译扣2分'等），你必须严格执行这些扣分指令。** 请在`scoring_basis`的“判断与理由”中清晰说明扣分的原因和依据，并在最终的`得分`或`itemized_scores`中体现扣分后的结果.\n\n"
            "【特殊情况处理】：\n若学生答案图片完全空白、字迹完全无法辨认，或所写内容与题目要求完全无关，请按以下规则填充JSON：\n"
            "    - `student_answer_summary`: 明确注明具体情况，例如：“学生未作答。”，“图片内容完全无法识别，字迹模糊不清。”，“学生答案内容与题目要求完全不符。”\n"
            "    - `scoring_basis`: 简要说明此判断的依据，例如：“答题区域空白，无任何作答痕迹。”\n"
            "    - `itemized_scores`:\n"
            "        - 对于按【得分点/答案要点/关键步骤】给分的题型，应输出一个与【评分细则】中预设的相应条目数量相同长度的全零列表（例如，若细则有3个得分点，则输出 `[0, 0, 0]`）。\n"
            "        - 对于整体评估的开放题型，应输出 `[0]`。\n"
            "    - `recognition_confidence`: {\"score\": \"1\", \"reason\": \"[对应上述特殊情况的理由，例如：图片空白或字迹完全无法识别。]\"}\n"
        )

    def _build_objective_fillintheblank_prompt(self, standard_answer_rubric):
        prompt_json = {
          "system_message": self._get_common_system_message(),
          "user_task": {
            "task_description": "请分析以下学生答案图片（客观填空题），并根据下方提供的评分细则给出评分。",
            "question_type_specific_instructions": "【题目类型：客观填空题】\n请仔细阅读【评分细则】中对【每一个填空项/答案要点】的具体标准答案、允许的表达方式及对应的分值。\n你的核心任务是判断学生对每个【填空项/答案要点】的回答是否符合细则要求。请严格遵照【评分细则】中关于答案细节（例如：格式、准确性等）的具体规定进行给分。若【评分细则】中包含灵活给分说明（如“意思对即可”），请在评分依据中体现你的理解。",
            "scoring_rubric_placeholder": standard_answer_rubric,
            "student_answer_image_placeholder": "（图片内容会通过API的其他方式传入，这里仅为逻辑占位）",
            "output_format_specification": {
              "description": "请严格按照以下JSON格式返回结果，不要包含任何额外的解释性文字在JSON结构之外。确保输出的是有效的JSON，不要添加任何前缀、后缀、代码块标记或自然语言描述。直接输出JSON对象。",
              "format": {
                "student_answer_summary": "【请在此处对图片中的学生手写答案进行**中立、客观、不带任何主观判断**的【核心内容概括】（例如：“学生答案的主要内容为[...]，表述清晰。”或“学生填写了[...]，部分内容无法识别。”）",
                "scoring_basis": "【请针对【评分细则】中的【每一个填空项/答案要点】，清晰地解释你是如何判断该填空项/答案要点的是否得分以及得了多少分的。你需要：\n1. 引用或概括学生在对应填空项/答案要点上的作答内容（如果未作答请说明）。\n2. 结合评分细则，说明你的评分判断和理由（例如：是否与标准答案一致，是否满足特定格式要求等）。\n3. 明确指出该填空项/答案要点你最终给了多少分。\n请确保你的解释能够清晰地支撑你在 \`itemized_scores\` 字段中给出的对应分数。\n",
                "itemized_scores": "【一个数字列表，例如 `[2, 0, 1]`。列表中的每个数字代表学生在【评分细则】中【对应顺序的每一个填空项/答案要点】上获得的【实际得分】。列表的长度应与评分细则中填空项/答案要点的数量一致。】",
                # 此版本暂时不启用置信度功能，今后如果需要再启用
                # "recognition_confidence": {
                #   "score": "[请从1-5中给出一个整数，代表你对本次图片中手写文字识别的自信程度。1=非常不确定，大量关键文字无法识别；3=基本可读，但部分文字模糊；5=非常自信，所有文字清晰可辨。]",
                #   "reason": "[请用一句话简述你给出该分数的原因，例如：'字迹清晰，无涂改。'或'部分关键名词书写较为潦草，识别存在不确定性。']"
                # }
              }
            }
          }
        }
        return json.dumps(prompt_json, ensure_ascii=False, indent=2)

    def _build_subjective_pointbased_prompt(self, standard_answer_rubric):
        prompt_json = {
          "system_message": self._get_common_system_message(),
          "user_task": {
            "task_description": "请分析以下学生答案图片（按点给分主观题），并根据下方提供的评分细则给出评分。",
            "question_type_specific_instructions": "【题目类型：按点给分主观题】\n请仔细阅读【评分细则】中列出的【每一个得分点】及其对应的原始分值。\n你的核心任务是判断学生的答案内容是否清晰、准确地覆盖了这些【得分点】的要求。请严格按照细则中对每个【得分点】的描述和要求进行判断和给分。如果细则中包含“意思对即可”“酌情给分”或类似的灵活给分说明，请在评分依据中体现你的理解。",
            "scoring_rubric_placeholder": standard_answer_rubric,
            "student_answer_image_placeholder": "（图片内容会通过API的其他方式传入，这里仅为逻辑占位）",
            "output_format_specification": {
              "description": "请严格按照以下JSON格式返回结果，不要包含任何额外的解释性文字在JSON结构之外。确保输出的是有效的JSON，不要添加任何前缀、后缀、代码块标记或自然语言描述。直接输出JSON对象。",
              "format": {
                "student_answer_summary": "【请在此处对图片中的学生手写答案进行**中立、客观、不带任何主观判断**的【核心内容概括】（例如：“学生答案的主要内容为[...]，表述清晰。”或“学生填写了[...]，部分内容无法识别。”）",
                "scoring_basis": "【请针对【评分细则】中的【每一个得分点】，清晰地解释你是如何判断该得分点的是否得分以及得了多少分的。你需要：\n1. 引用或概括学生在对应得分点上的作答内容（如果未作答请说明）。\n2. 结合评分细则，说明你的评分判断和理由。\n3. 明确指出该得分点你最终给了多少分。\n请确保你的解释能够清晰地支撑你在 `itemized_scores` 字段中给出的对应分数。\n",
                "itemized_scores": "【一个数字列表，例如 `[3, 1, 0, 2]`。列表中的每个数字代表学生在【评分细则】中【对应顺序的每一个得分点】上获得的【实际得分】。列表的长度应与评分细则中得分点的数量一致。】",
                # 此版本暂时不启用置信度功能，今后如果需要再启用
                # "recognition_confidence": {
                #   "score": "[请从1-5中给出一个整数，代表你对本次图片中手写文字识别的自信程度。1=非常不确定，大量关键文字无法识别；3=基本可读，但部分文字模糊；5=非常自信，所有文字清晰可辨。]",
                #   "reason": "[请用一句话简述你给出该分数的原因，例如：'字迹清晰，无涂改。'或'部分关键名词书写较为潦草，识别存在不确定性。']"
                # }
              }
            }
          }
        }
        return json.dumps(prompt_json, ensure_ascii=False, indent=2)

    def _build_formula_proof_prompt(self, standard_answer_rubric):
        prompt_json = {
          "system_message": self._get_common_system_message(),
          "user_task": {
            "task_description": "请分析以下学生答案图片（公式计算/证明题），并根据下方提供的评分细则给出评分。",
            "question_type_specific_instructions": "【题目类型：公式计算/证明题】\n请仔细阅读【评分细则】中对【解题的每一个关键步骤/采分点、所用公式的准确性、计算结果的正确性、证明逻辑的严密性以及数学/物理/化学符号和书写的规范性】的具体要求和分值分配。\n你的核心任务是逐一核对学生的解题过程和最终答案是否符合细则中每一个【关键步骤/采分点】的标准。",
            "scoring_rubric_placeholder": standard_answer_rubric,
            "student_answer_image_placeholder": "（图片内容会通过API的其他方式传入，这里仅为逻辑占位）",
            "output_format_specification": {
              "description": "请严格按照以下JSON格式返回结果，不要包含任何额外的解释性文字在JSON结构之外。确保输出的是有效的JSON，不要添加任何前缀、后缀、代码块标记或自然语言描述。直接输出JSON对象。",
              "format": {
                "student_answer_summary": "【请在此处对图片中的学生手写答案进行**中立、客观、不带任何主观判断**的【核心内容概括】（例如：“学生答案的主要内容为[...]，表述清晰。”或“学生填写了[...]，部分内容无法识别。”）",
                "scoring_basis": "【请针对【评分细则】中的【每一个关键步骤/采分点】，清晰地解释你是如何判断该步骤/采分点的是否得分以及得了多少分的。你需要：\n1. 引用或概括学生在对应步骤/采分点上的解题过程或书写内容（如果未作答或跳过请说明）。\n2. 结合评分细则，说明你的评分判断和理由（例如：公式是否正确，代入是否无误，计算结果是否准确，证明逻辑是否严密等）。\n3. 明确指出该步骤/采分点你最终给了多少分。\n请确保你的解释能够清晰地支撑你在 `itemized_scores` 字段中给出的对应分数。\n",
                "itemized_scores": "【一个数字列表，例如 `[2, 2, 0, 1]`。列表中的每个数字代表学生在【评分细则】中【对应顺序的每一个关键步骤/采分点】上获得的【实际得分】。列表的长度应与评分细则中关键步骤/采分点的数量一致。】",
                # 此版本暂时不启用置信度功能，今后如果需要再启用
                # "recognition_confidence": {
                #   "score": "[请从1-5中给出一个整数，代表你对本次图片中手写文字识别的自信程度。1=非常不确定，大量关键文字无法识别；3=基本可读，但部分文字模糊；5=非常自信，所有文字清晰可辨。]",
                #   "reason": "[请用一句话简述你给出该分数的原因，例如：'字迹清晰，无涂改。'或'部分关键名词书写较为潦草，识别存在不确定性。']"
                # }
              }
            }
          }
        }
        return json.dumps(prompt_json, ensure_ascii=False, indent=2)

    def _build_holistic_evaluation_prompt(self, standard_answer_rubric):
        prompt_json = {
          "system_message": self._get_common_system_message(),
          "user_task": {
            "task_description": "请分析以下学生答案图片（整体评估开放题，如作文、论述等），并根据下方提供的评分细则给出评分。",
            "question_type_specific_instructions": "【题目类型：整体评估开放题】\n请仔细阅读【评分细则】中关于【各项评估维度/评分标准或等级描述】（例如：内容是否切题、思想深度、结构逻辑性、语言表达的准确性与文采、观点创新性、书写规范等）。\n你的核心任务是基于这些宏观标准，对学生的答案进行全面的、综合的判断，并给出一个最终总分。请在评分依据中清晰阐述你是如何结合细则中的各个评估维度得出该总分的。",
            "scoring_rubric_placeholder": standard_answer_rubric,
            "student_answer_image_placeholder": "（图片内容会通过API的其他方式传入，这里仅为逻辑占位）",
            "output_format_specification": {
              "description": "请严格按照以下JSON格式返回结果，不要包含任何额外的解释性文字在JSON结构之外。确保输出的是有效的JSON，不要添加任何前缀、后缀、代码块标记或自然语言描述。直接输出JSON对象。",
              "format": {
                "student_answer_summary": "【请在此处对图片中的学生手写答案进行**中立、客观、不带任何主观判断**的【核心内容概括】（例如：“学生答案的主要内容为[...]，表述清晰。”或“学生填写了[...]，部分内容无法识别。”）",
                "scoring_basis": "【请在此处综合阐述你给出 `itemized_scores` 中最终总分的详细理由。你需要：\n1. 参照【评分细则】中列出的各项整体评估维度（例如：内容切题性、思想深度、结构逻辑、语言表达、书写规范等）。\n2. 针对每一个主要评估维度，清晰描述从图片中观察到的学生表现。\n3. 解释这些不同维度的表现是如何共同作用，最终形成了你在 `itemized_scores` 中给出的那个总分。\n请确保你的阐述逻辑清晰、依据充分，并直接关联到最终的评分结果。\n（例如：\n- 维度1（如内容切题性）：学生表现[...具体描述...]，符合/不符合细则的[...某标准...]。\n- 维度2（如结构逻辑）：学生表现[...具体描述...]，符合/不符合细则的[...某标准...]。\n- (依此类推所有主要维度)\n- 综合评价：基于以上各维度表现，[简述如何综合考虑，例如哪些是主要影响因素]，并对照评分细则中的等级描述，最终评定总分为XX分。）",
                "itemized_scores": "【一个【只包含一个数字的列表】，例如 `[45]` 或 `[8]`。这个数字代表你根据【评分细则】中的整体评估标准/评分维度给出的【最终总分】。】",
                # 此版本暂时不启用置信度功能，今后如果需要再启用
                # "recognition_confidence": {
                #   "score": "[请从1-5中给出一个整数，代表你对本次图片中手写文字识别的自信程度。1=非常不确定，大量关键文字无法识别；3=基本可读，但部分文字模糊；5=非常自信，所有文字清晰可辨。]",
                #   "reason": "[请用一句话简述你给出该分数的原因，例如：'字迹清晰，无涂改。'或'部分关键名词书写较为潦草，识别存在不确定性。']"
                # }
              }
            }
          }
        }
        return json.dumps(prompt_json, ensure_ascii=False, indent=2)

    def select_and_build_prompt(self, standard_answer, question_type):
        """
        根据题目类型选择并构建相应的Prompt。
        """
        # 确保 standard_answer 是字符串类型，如果不是，尝试转换或记录错误
        if not isinstance(standard_answer, str):
            self.log_signal.emit(f"评分细则不是字符串类型 (实际类型: {type(standard_answer)})，尝试转换。", True)
            try:
                standard_answer = str(standard_answer) # 尝试转换
            except Exception as e:
                error_msg = f"评分细则无法转换为字符串 (错误: {e})，阅卷已暂停，请检查配置并手动处理当前题目。"
                self.log_signal.emit(error_msg, True)
                self._set_error_state(error_msg)
                return None # 中断处理

        # 再次检查 standard_answer 是否有效 (可能转换后仍为空或在初始就是空)
        if not standard_answer or not standard_answer.strip():
            error_msg = "评分细则为空或仅包含空白，阅卷已暂停，请检查配置并手动处理当前题目。"
            self.log_signal.emit(error_msg, True)
            self._set_error_state(error_msg)
            return None # 中断处理


        if question_type == "Objective_FillInTheBlank": # 更新了类型名称
            return self._build_objective_fillintheblank_prompt(standard_answer)
        elif question_type == "Subjective_PointBased_QA":
            return self._build_subjective_pointbased_prompt(standard_answer)
        elif question_type == "Formula_Proof_StepBased":
            return self._build_formula_proof_prompt(standard_answer)
        elif question_type == "Holistic_Evaluation_Open":
            return self._build_holistic_evaluation_prompt(standard_answer)
        else:
            self.log_signal.emit(f"未知的题目类型: '{question_type}'，将使用默认的按点给分主观题Prompt。", True)
            return self._build_subjective_pointbased_prompt(standard_answer)
    # --- 结束新增的Prompt构建方法 ---

    def _set_error_state(self, reason):
        """统一设置错误状态"""
        self.completion_status = "error"
        self.interrupt_reason = reason
        self.running = False
        self.log_signal.emit(f"错误: {reason}", True)

    def run(self):
        """线程主函数，执行自动阅卷流程"""
        # 重置状态
        self.completion_status = "running"
        self.completed_count = 0
        self.interrupt_reason = ""
        self.running = True
        self.log_signal.emit("自动阅卷线程已启动", False, "INFO")

        try:
            # 获取参数
            cycle_number = self.parameters.get('cycle_number', 1)
            wait_time = self.parameters.get('wait_time', 1)
            question_configs = self.parameters.get('question_configs', [])
            dual_evaluation = self.parameters.get('dual_evaluation', False)
            score_diff_threshold = self.parameters.get('score_diff_threshold', 10)

            if not question_configs:
                self._set_error_state("未配置题目信息")
                return

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
                if not img_str:
                    if not self.running: break
                    continue

                # 构建JSON Prompt
                self.log_signal.emit(f"为第 {question_index} 题 (类型: {question_type}) 构建Prompt...", False, "DETAIL")
                text_prompt_for_api = self.select_and_build_prompt(standard_answer, question_type)

                if text_prompt_for_api is None:
                    if not self.running: break
                    continue

                # 调用API进行评分
                eval_result = self.evaluate_answer(
                    img_str, text_prompt_for_api, q_config, dual_evaluation, score_diff_threshold
                )

                # 检查是否完全失败
                if eval_result is None:
                    if not self.running: break
                    continue

                score, reasoning_data, itemized_scores_data, confidence_data, raw_ai_response = eval_result

                # 如果评分处理失败，仍然记录错误信息，但不输入分数
                if score is None:
                    self.log_signal.emit(f"第 {question_index} 题评分失败，将记录错误信息但跳过分数输入", True)
                    self.record_grading_result(question_index, 0, img_str, reasoning_data, itemized_scores_data, confidence_data)
                    if not self.running: break
                    continue

                # 输入分数
                self.input_score(score, score_input_pos, confirm_button_pos, q_config)

                if not self.running:
                    break

                # 更新进度
                self.completed_count = i + 1
                total = cycle_number
                self.progress_signal.emit(self.completed_count, total)

                # 记录阅卷结果
                self.record_grading_result(question_index, score, img_str, reasoning_data, itemized_scores_data, confidence_data, raw_ai_response)

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

        except Exception as e:
            error_detail = traceback.format_exc()
            self.log_signal.emit(f"自动阅卷出错: {str(e)}\n{error_detail}", True)
            self.completion_status = "error"
            self.interrupt_reason = f"系统错误: {str(e)}"

        finally:
            self.running = False
            # 先生成汇总记录
            try:
                self.generate_summary_record(cycle_number, dual_evaluation, score_diff_threshold, elapsed_time)
            except Exception as summary_error:
                self.log_signal.emit(f"生成汇总记录失败: {str(summary_error)}", True)

            # 再发送信号
            if self.completion_status == "completed":
                self.finished_signal.emit()
            elif self.completion_status == "threshold_exceeded":
                self.threshold_exceeded_signal.emit(self.interrupt_reason or "双评分差超过阈值")
            else:
                self.error_signal.emit(self.interrupt_reason or "未知错误")

    def set_parameters(self, **kwargs):
        """设置线程参数"""
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
        """停止线程"""
        self.running = False
        if self.completion_status == "running":
            self.completion_status = "error"
            self.interrupt_reason = "用户手动停止"
        self.log_signal.emit("正在停止自动阅卷线程...", False)

    def capture_answer_area(self, area):
        """截取答案区域

        Args:
            area: 答案区域坐标 (x, y, width, height)

        Returns:
            base64编码的图片字符串
        """
        try:
            x, y, width, height = area

            # 确保宽度和高度为正值
            if width < 0:
                x = x + width
                width = abs(width)
            if height < 0:
                y = y + height
                height = abs(height)

            # 截取屏幕指定区域
            screenshot = ImageGrab.grab(bbox=(x, y, x + width, y + height))

            # 转换为带Data URI前缀的base64字符串
            buffered = BytesIO()
            screenshot.save(buffered, format="JPEG")
            base64_data = base64.b64encode(buffered.getvalue()).decode()
            img_str = f"data:image/jpeg;base64,{base64_data}"

            return img_str
        except Exception as e:
            self._set_error_state(f"截取答案区域出错: {str(e)}")
            return None


    def evaluate_answer(self, img_str, prompt, current_question_config, dual_evaluation=False, score_diff_threshold=10):
        """
        评估答案（重构后）。
        协调API调用和响应处理，支持单评和双评模式。
        """
        # 调用第一个API并处理结果
        score1, reasoning1, scores1, confidence1, response_text1, error1 = self._call_and_process_single_api(
            self.api_service.call_first_api,
            img_str,
            prompt,
            current_question_config,
            api_name="第一个API"
        )
        if error1:
            self._set_error_state(error1)
            return None, error1, None, None

        # 如果不启用双评，直接返回第一个API的结果
        if not dual_evaluation:
            return score1, reasoning1, scores1, confidence1, response_text1

        # 如果启用双评，继续调用第二个API
        score2, reasoning2, scores2, confidence2, error2 = self._call_and_process_single_api(
            self.api_service.call_second_api,
            img_str,
            prompt,
            current_question_config,
            api_name="第二个API"
        )
        if error2:
            self._set_error_state(error2)
            return None, error2, None, None

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
            return None, error_dual, None, None

        return final_score, combined_reasoning, combined_scores, combined_confidence


    def _call_and_process_single_api(self, api_call_func, img_str, prompt, q_config, api_name="API", max_retries=3):
        """
        调用指定的API函数，并处理其响应。支持重试机制以提高稳定性。

        Args:
            api_call_func: 要调用的API服务方法 (e.g., self.api_service.call_first_api)
            img_str: 图片base64字符串
            prompt: 提示词
            q_config: 当前题目配置
            api_name: 用于日志的API名称
            max_retries: 最大重试次数，默认3次

        Returns:
            一个元组 (score, reasoning, itemized_scores, confidence, error_message)
        """
        for attempt in range(max_retries):
            if attempt > 0:
                self.log_signal.emit(f"{api_name}第{attempt}次重试...", False)
                time.sleep(1)  # 短暂延迟，避免过于频繁的请求

            self.log_signal.emit(f"正在调用{api_name}进行评分... (尝试 {attempt + 1}/{max_retries})", False)
            response_text, error_from_call = api_call_func(img_str, prompt)

            if error_from_call or not response_text:
                error_msg = f"{api_name}调用失败或响应为空: {error_from_call}"
                if attempt == max_retries - 1:  # 最后一次尝试失败
                    self.log_signal.emit(error_msg, True)
                    return None, None, None, None, response_text, error_msg
                else:
                    self.log_signal.emit(f"{error_msg}，准备重试...", True)
                    continue

            success, result_data = self.process_api_response((response_text, None), q_config)

            if success:
                score, reasoning, itemized_scores, confidence = result_data
                return score, reasoning, itemized_scores, confidence, response_text, None
            else:
                error_info = result_data
                if attempt == max_retries - 1:  # 最后一次尝试的处理失败
                    error_msg = f"{api_name}评分处理失败（已重试{max_retries}次）。错误: {error_info}"
                    self.log_signal.emit(error_msg, True)
                    return None, None, None, None, response_text, error_msg
                else:
                    self.log_signal.emit(f"{api_name}处理失败: {error_info}，准备重试...", True)
                    continue

        # 理论上不会到达这里，但为了安全
        return None, None, None, None, f"{api_name}重试后仍失败"

    def _handle_dual_evaluation(self, result1, result2, score_diff_threshold):
        """
        处理双评逻辑，比较分数，合并结果。

        Args:
            result1: 第一个API的处理结果元组 (score, reasoning, itemized_scores, confidence)
            result2: 第二个API的处理结果元组 (score, reasoning, itemized_scores, confidence)
            score_diff_threshold: 分差阈值

        Returns:
            一个元组 (final_score, combined_reasoning, combined_scores, combined_confidence, error_message)
        """
        score1, reasoning1, itemized_scores1, confidence1 = result1
        score2, reasoning2, itemized_scores2, confidence2 = result2

        score_diff = abs(score1 - score2)
        self.log_signal.emit(f"API-1得分: {score1}, API-2得分: {score2}, 分差: {score_diff}", False)

        if score_diff > score_diff_threshold:
            error_msg = f"双评分差过大: {score_diff:.2f} > {score_diff_threshold}"
            self.log_signal.emit(f"分差 {score_diff:.2f} 超过阈值 {score_diff_threshold}，停止运行", True)
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
            self.log_signal.emit(error_msg, True)
            return False, error_msg

        try:
            self.log_signal.emit("尝试解析API响应JSON...", False)

            # 首先尝试直接解析
            data = None
            try:
                data = json.loads(response_text)
            except json.JSONDecodeError:
                # 如果直接解析失败，尝试提取JSON部分
                self.log_signal.emit("直接解析失败，尝试提取JSON部分...", False)
                extracted_json = self._extract_json_from_text(response_text)
                if extracted_json:
                    try:
                        data = json.loads(extracted_json)
                        self.log_signal.emit("成功从响应中提取并解析JSON", False)
                    except json.JSONDecodeError:
                        pass  # 仍然失败，继续到外层的异常处理

            if data is None:
                raise json.JSONDecodeError("无法解析响应为JSON", response_text, 0)

            student_answer_summary = data.get("student_answer_summary", "未能提取学生答案摘要")
            scoring_basis = data.get("scoring_basis", "未能提取评分依据")
            itemized_scores_from_json = data.get("itemized_scores")
            confidence_data = {}  # 置信度功能暂时停用

            self.log_signal.emit(f"AI回复摘要: {student_answer_summary}", False, "RESULT")
            self.log_signal.emit(f"AI评分依据: {scoring_basis}", False, "RESULT")

            calculated_total_score = 0.0
            numeric_scores_list_for_return = []

            if itemized_scores_from_json is None or not isinstance(itemized_scores_from_json, list):
                error_msg = "API响应中'itemized_scores'缺失或格式错误 (应为列表)"
                self.log_signal.emit(error_msg, True)
                return False, error_msg

            if not itemized_scores_from_json:
                self.log_signal.emit("分项得分列表为空，判定总分为0。", False)
                calculated_total_score = 0.0
                numeric_scores_list_for_return = []
            else:
                try:
                    numeric_scores_list_for_return = [float(s) for s in itemized_scores_from_json]
                    calculated_total_score = sum(numeric_scores_list_for_return)
                except (ValueError, TypeError) as e_sum:
                    error_msg = f"API返回的分项得分 '{itemized_scores_from_json}' 包含无效内容或解析失败 (错误: {e_sum})"
                    self.log_signal.emit(error_msg, True)
                    return False, error_msg

            self.log_signal.emit(f"根据itemized_scores计算得到的原始总分: {calculated_total_score}", False)

            final_score = self._validate_and_finalize_score(calculated_total_score, current_question_config)

            if final_score is None:
                error_msg = "分数校验失败或超出范围"
                return False, error_msg

            reasoning_tuple = (student_answer_summary, data.get("scoring_basis", "未能提取评分依据"))

            result = (final_score, reasoning_tuple, numeric_scores_list_for_return, confidence_data)
            return True, result

        except json.JSONDecodeError as e_json:
            error_msg = ("【API响应格式错误】模型返回的内容不是标准的JSON，无法解析。\n"
                         "可能原因：\n"
                         "1. 模型可能正忙或出现内部错误，导致输出了非结构化文本。\n"
                         "2. 您使用的模型可能不完全兼容当前Prompt的JSON输出要求。\n"
                         "解决方案：请尝试重新运行。如果问题反复出现，建议更换模型或检查供应商服务状态。")
            self.log_signal.emit(f"{error_msg}\n原始响应(前200字符): '{response_text[:200]}...'", True)
            return False, (error_msg, response_text)
        except (KeyError, IndexError) as e_key:
            error_msg = (f"【API响应结构错误】模型返回的JSON中缺少关键信息 (如: {str(e_key)})。\n"
                         f"可能原因：\n"
                         f"1. 模型未能完全遵循格式化输出的指令。\n"
                         f"2. API供应商可能更新了其响应结构。\n"
                         f"解决方案：这是程序需要处理的兼容性问题。请将此错误反馈给开发者。")
            self.log_signal.emit(f"{error_msg}\n完整响应: {response_text}", True)
            return False, error_msg
        except Exception as e_process:
            error_detail = traceback.format_exc()
            error_msg = f"处理API响应时发生未知错误: {str(e_process)}\n{error_detail}"
            self.log_signal.emit(error_msg, True)
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
                self.log_signal.emit(error_msg, True)
                self._set_error_state(error_msg)
                return None

            final_score = float(total_score_from_json)

            # 范围校验与满分截断
            if final_score < q_min_score:
                self.log_signal.emit(f"计算总分 {final_score} 低于最低分 {q_min_score}，修正为 {q_min_score}。", True)
                final_score = q_min_score
            elif final_score > q_max_score:
                self.log_signal.emit(f"计算总分 {final_score} 超出题目满分 {q_max_score} (原始AI总分: {total_score_from_json})，将修正为满分 {q_max_score}。", True)
                final_score = q_max_score # 修正为满分

            self.log_signal.emit(f"AI原始总分: {total_score_from_json}, 校验后最终得分: {final_score}", False)
            return final_score

        except Exception as e:
            error_detail = traceback.format_exc()
            error_msg = f"校验和处理分数时发生严重错误: {str(e)}\n{error_detail}"
            self.log_signal.emit(error_msg, True)
            self._set_error_state(error_msg)
            return None

    def _extract_json_from_text(self, text):
        """
        从文本中提取JSON字符串。
        尝试找到第一个完整的JSON对象（以{开始，以}结束）。
        增强版：处理常见的AI响应格式问题。
        """
        import re
        try:
            # 清理常见的AI响应前缀和后缀
            text = text.strip()

            # 移除常见的markdown代码块标记
            text = re.sub(r'^```\s*json\s*', '', text, flags=re.IGNORECASE)
            text = re.sub(r'^```\s*', '', text)
            text = re.sub(r'```\s*$', '', text)

            # 移除可能的解释性文字
            # 查找可能的JSON开始位置
            start_pos = text.find('{')
            if start_pos != -1:
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
            self.log_signal.emit(f"输入位置未配置，无法输入分数 {score_value}", True)
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
            self.log_signal.emit(f"执行单次输入到 ({input_pos[0]},{input_pos[1]}) 出错: {str(e)}", True)
            # self._set_error_state(f"执行单次输入出错: {str(e)}") # 避免重复设置错误
            return False

    def input_score(self, final_score_to_input: float, default_score_pos, confirm_button_pos, current_question_config):
        """输入分数，根据模式选择单点或三步输入，并处理分数到0.5的倍数。"""
        try:
            input_successful = False
            current_processing_q_index = current_question_config.get('question_index', self.api_service.current_question_index)
            q_enable_three_step_scoring = current_question_config.get('enable_three_step_scoring', False)
            q_max_score = float(current_question_config.get('max_score', self.max_score)) # 确保是浮点数, 使用线程级默认最高分

            # 1. 将AI给出的分数处理到最近的0.5的倍数
            final_score_processed = round_to_nearest_half(final_score_to_input)
            
            # 获取当前题目的最小分值，用于最终修正 (q_max_score 已在上方获取并更新为使用 self.max_score 作为默认值)
            q_min_score = float(current_question_config.get('min_score', self.min_score)) 

            self.log_signal.emit(f"AI得分 (原始范围 [{q_min_score}-{q_max_score}]): {final_score_to_input}, 四舍五入到0.5倍数后: {final_score_processed}", False)

            # 2. 修正四舍五入后的分数，确保其严格在 [q_min_score, q_max_score] 范围内
            #    final_score_to_input 已经由 _validate_and_finalize_score 保证在原始 [min_score, max_score] 内。
            #    这里的 final_score_processed 是 round_to_nearest_half(final_score_to_input) 的结果。

            if final_score_processed < q_min_score:
                self.log_signal.emit(f"四舍五入到0.5倍数后得分 ({final_score_processed}) 低于题目最低分 ({q_min_score})，将修正为最低分: {q_min_score}。", True)
                final_score_processed = q_min_score
            elif final_score_processed > q_max_score: # 使用 elif
                self.log_signal.emit(f"四舍五入到0.5倍数后得分 ({final_score_processed}) 高于题目满分 ({q_max_score})，将修正为满分: {q_max_score}。", True)
                final_score_processed = q_max_score

            # 3. 根据模式进行分数输入
            if (current_processing_q_index == 1 and
                q_enable_three_step_scoring and
                self.is_single_question_one_run):

                self.log_signal.emit(f"第一题启用三步分数输入模式，目标总分: {final_score_processed}", False)

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
                self.log_signal.emit(f"三步拆分结果: s1={s1}, s2={s2}, s3={s3} (总和: {round_to_nearest_half(s1+s2+s3)})", False)

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
                self.log_signal.emit(f"标准单点输入模式 (题目 {current_processing_q_index})，得分: {final_score_processed}", False)
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
                self.log_signal.emit(f"已输入总分: {final_score_processed} (题目 {current_processing_q_index}) 并点击确认", False)
            # else 分支的错误已在各自的输入逻辑中通过 return 处理，或由 self.running 状态控制

        except Exception as e:
            error_detail = traceback.format_exc()
            self.log_signal.emit(f"输入分数过程中发生严重错误: {str(e)}\n{error_detail}", True)
            if self.running: # 避免在已停止时重复设置错误
                self._set_error_state(f"输入分数严重错误: {str(e)}")

    def record_grading_result(self, question_index, score, img_str, reasoning_data, itemized_scores_data, confidence_data, raw_ai_response=None):
        """记录阅卷结果，并发送信号 (重构后)"""
        try:
            # 1. 构建基础记录字典
            record = {
                'timestamp': datetime.datetime.now().strftime('%Y年%m月%d日_%H点%M分%S秒'),
                'record_type': 'detail',
                'question_index': question_index,
                'total_score': score,
                'is_dual_evaluation_run': self.parameters.get('dual_evaluation', False),
                'total_questions_in_run': self.total_question_count_in_run,
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

            elif isinstance(reasoning_data, tuple) and len(reasoning_data) == 2:
                # 检查是否为错误模式 (error_msg, raw_response)
                first_elem, second_elem = reasoning_data
                if isinstance(second_elem, str) and not second_elem.startswith('{') and not second_elem.startswith('['):
                    # 这是错误模式，second_elem是原始响应
                    error_info = first_elem
                    raw_response = second_elem
                    record.update({
                        'student_answer': "评分失败",
                        'reasoning_basis': error_info,
                        'raw_ai_response': raw_response,
                        'sub_scores': "AI未提供",
                    })
                    self.log_signal.emit(f"记录结果时检测到解析错误，已保存原始AI响应", True)
                else:
                    # 单评成功模式
                    summary, basis = reasoning_data
                    record.update({
                        'student_answer': summary,
                        'reasoning_basis': basis,
                        'sub_scores': str(itemized_scores_data),
                        'raw_ai_response': raw_ai_response,
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
                })
                self.log_signal.emit(f"记录结果时遇到未预期的reasoning_data格式或错误: {error_info}", True)

            # 3. 发送信号
            self.record_signal.emit(record)
            self.log_signal.emit(f"第 {question_index} 题阅卷记录已发送。最终得分: {score}", False)

        except Exception as e:
            error_detail = traceback.format_exc()
            self.log_signal.emit(f"记录阅卷结果时发生严重错误: {str(e)}\n{error_detail}", True)

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
        self.log_signal.emit("阅卷汇总记录已发送。", False)
