# --- START OF FILE main_window.py ---

import sys
import os
import traceback
from PyQt5.QtWidgets import (QMainWindow, QWidget, QMessageBox, QDialog,
                             QComboBox, QLineEdit, QCheckBox, QSpinBox,
                             QPlainTextEdit, QApplication, QShortcut, QLabel, QPushButton)
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QKeySequence, QFont
from PyQt5 import uic

# --- 新增导入 ---
# 从 api_service.py 导入转换函数和UI文本列表生成函数
from api_service import get_provider_id_from_ui_text, get_ui_text_from_provider_id, UI_TEXT_TO_PROVIDER_ID

class MainWindow(QMainWindow):
    # 日志级别定义
    LOG_LEVEL_INFO = "INFO"      # 基本信息
    LOG_LEVEL_DETAIL = "DETAIL"  # 详细处理信息
    LOG_LEVEL_RESULT = "RESULT"  # AI评分结果
    LOG_LEVEL_ERROR = "ERROR"    # 错误信息

    # ... (信号定义部分保持不变) ...
    # update_signal = pyqtSignal(str)
    log_signal = pyqtSignal(str, bool, str)  # message, is_error, level
    progress_signal = pyqtSignal(int, int)
    finished_signal = pyqtSignal()

    def __init__(self, config_manager, api_service, worker):
        super().__init__()
        self.config_manager = config_manager
        self.api_service = api_service
        self.worker = worker
        self._is_initializing = True

        # ... (UI文件加载部分保持不变) ...
        if getattr(sys, 'frozen', False):
            base_path = sys._MEIPASS
        else:
            base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ui_path = os.path.join(base_path, "setting", "单题.ui")
        uic.loadUi(ui_path, self)

        # ... (其他初始化属性保持不变) ...
        self.answer_windows = {}
        self.current_question = 1
        self.max_questions = 1  # 单题模式只处理第一题
        self.shortcut_esc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self.shortcut_esc.activated.connect(self.stop_auto_thread)
        self._ui_cache = {}

        self.init_ui()

        # 添加缓存UI元素
        self.cache_status_label = QLabel("")
        self.cache_status_label.setStyleSheet("color: orange; font-weight: bold;")
        self.merge_cache_button = QPushButton("添加最新阅卷记录")
        self.merge_cache_button.setStyleSheet("QPushButton { background-color: #4CAF50; color: white; padding: 8px 16px; border: none; border-radius: 4px; }"
                                               "QPushButton:hover { background-color: #45a049; }"
                                               "QPushButton:pressed { background-color: #3e8e41; }"
                                               "QPushButton:disabled { background-color: #cccccc; color: #666666; }")
        self.merge_cache_button.clicked.connect(self.request_merge_cache)
        self.merge_cache_button.hide()

        # 查找UI中的合适区域添加缓存控件（假设有一个水平布局区域）
        # 这里需要根据实际UI文件找到合适的位置，比如日志区域上方
        # 临时添加到一个假设的位置，实际使用时需要调整
        from PyQt5.QtWidgets import QHBoxLayout, QVBoxLayout
        # 假设有log_text布局的父级，添加缓存区域
        log_widget = self.get_ui_element('log_text')
        if log_widget and log_widget.parent():
            parent_layout = log_widget.parent().layout()
            if parent_layout:
                # 创建水平布局添加缓存控件
                cache_layout = QHBoxLayout()
                cache_layout.addWidget(self.cache_status_label)
                cache_layout.addStretch()
                cache_layout.addWidget(self.merge_cache_button)

                # 将缓存布局插入到日志上方
                if hasattr(parent_layout, 'insertLayout'):
                    parent_layout.insertLayout(parent_layout.count() - 1, cache_layout)

        self.show()
        self._is_initializing = False
        self.log_message("主窗口初始化完成")

    # ==========================================================================
    #  核心修改：配置处理逻辑
    # ==========================================================================

    def handle_lineEdit_save(self, field_name, value):
        if self._is_initializing: return
        self.config_manager.update_config_in_memory(field_name, value)
        self.log_message(f"配置项 '{field_name}' 更新为: {value}")

    def handle_plainTextEdit_save(self, field_name, value):
        if self._is_initializing: return
        self.config_manager.update_config_in_memory(field_name, value)
        # 答案内容较长，日志可以简洁些
        self.log_message(f"配置项 '{field_name}' 已更新")

    def handle_spinBox_save(self, field_name, value):
        if self._is_initializing: return
        self.config_manager.update_config_in_memory(field_name, value)
        self.log_message(f"配置项 '{field_name}' 更新为: {value}")
    
    # --- 统一的 ComboBox 处理函数 ---
    def handle_comboBox_save(self, combo_box_name, ui_text):
        """统一的ComboBox保存处理"""
        if self._is_initializing: return

        if combo_box_name in ['first_api_url', 'second_api_url']:
            # 处理API provider ComboBox
            provider_id = get_provider_id_from_ui_text(ui_text)
            if not provider_id:
                self.log_message(f"错误: 无法识别的供应商 '{ui_text}'", is_error=True)
                return
            field_name = 'first_api_provider' if combo_box_name == 'first_api_url' else 'second_api_provider'
            self.config_manager.update_config_in_memory(field_name, provider_id)
            self.log_message(f"配置项 '{field_name}' 更新为: {provider_id} ({ui_text})")
        else:
            # 处理普通ComboBox（如subject_text）
            field_name = combo_box_name.replace('_text', '')  # subject_text -> subject
            self.config_manager.update_config_in_memory(field_name, ui_text)
            self.log_message(f"配置项 '{field_name}' 更新为: {ui_text}")

    def handle_checkBox_save(self, field_name, state):
        if self._is_initializing: return
        value = bool(state)
        self.config_manager.update_config_in_memory(field_name, value)
        self.log_message(f"配置项 '{field_name}' 更新为: {value}")

    def _connect_direct_edit_save_signals(self):
        """连接UI控件信号到即时保存处理函数"""
        # API Key 和 Model ID 字段
        for field_name in ['first_api_key', 'first_modelID', 'second_api_key', 'second_modelID']:
            widget = self.get_ui_element(field_name, QLineEdit)
            if widget:
                widget.editingFinished.connect(
                    lambda field=field_name, w=widget: self.handle_lineEdit_save(field, w.text())
                )
        
        # --- 统一的 ComboBox 信号连接 ---
        combo_boxes = ['first_api_url', 'second_api_url', 'subject_text']
        for combo_name in combo_boxes:
            widget = self.get_ui_element(combo_name, QComboBox)
            if widget:
                widget.currentTextChanged.connect(
                    lambda text, name=combo_name: self.handle_comboBox_save(name, text)
                )

        # 其他控件的信号连接保持不变...
        for field, w_type in [('cycle_number', QSpinBox), ('wait_time', QSpinBox)]:
            widget = self.get_ui_element(field, w_type)
            if widget:
                widget.valueChanged.connect(
                    lambda val, f=field: self.handle_spinBox_save(f, val)
                )

        for i in range(1, self.max_questions + 1):
            std_answer_widget = self.get_ui_element(f'StandardAnswer_text_{i}', QPlainTextEdit)
            if std_answer_widget:
                self._connect_plain_text_edit_save_signal(std_answer_widget, i)

    # --- eventFilter 和 _connect_plain_text_edit_save_signal 保持不变 ---
    def _connect_plain_text_edit_save_signal(self, widget, question_index):
        widget.setProperty('question_index', question_index)
        widget.setProperty('needs_save_on_focus_out', True)
        widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        if (event.type() == event.FocusOut and
            hasattr(obj, 'property') and
            obj.property('needs_save_on_focus_out')):
            q_index = obj.property('question_index')
            field_name = f"question_{q_index}_standard_answer"
            self.handle_plainTextEdit_save(field_name, obj.toPlainText())
        return super().eventFilter(obj, event)

    # ==========================================================================
    #  UI初始化和加载逻辑
    # ==========================================================================

    def init_ui(self):
        """初始化UI组件和布局"""
        # --- 核心修改: 动态填充 ComboBox ---
        provider_ui_texts = list(UI_TEXT_TO_PROVIDER_ID.keys())
        for combo_name in ['first_api_url', 'second_api_url']:
            combo_box = self.get_ui_element(combo_name, QComboBox)
            if combo_box:
                combo_box.clear()
                combo_box.addItems(provider_ui_texts)

        self.setup_question_selector()
        # ... 其他 setup 方法 ...
        self.setup_text_fields()
        self.setup_dual_evaluation()

        self.load_config_to_ui()
        self._connect_signals() # <--- 在这里统一调用

        self.log_message("UI组件初始化完成")
    
    def load_config_to_ui(self):
        """将配置从ConfigManager加载到UI控件"""
        if self._is_initializing and hasattr(self, '_config_loaded_once'): return
        self.log_message("正在加载配置到UI...")
        self._is_initializing = True

        try:
            # 加载 API Key 和 Model ID
            for field in ['first_api_key', 'first_modelID', 'second_api_key', 'second_modelID']:
                widget = self.get_ui_element(field, QLineEdit)
                if widget:
                    widget.setText(getattr(self.config_manager, field, ""))
            
            # --- 核心修改: 加载 Provider 并设置 ComboBox ---
            provider_map = {
                'first_api_url': self.config_manager.first_api_provider,
                'second_api_url': self.config_manager.second_api_provider,
            }
            for combo_name, provider_id in provider_map.items():
                combo_box = self.get_ui_element(combo_name, QComboBox)
                if combo_box:
                    # 将内部ID (如 "volcengine") 转换为UI文本 (如 "火山引擎 (推荐)")
                    ui_text_to_select = get_ui_text_from_provider_id(provider_id)
                    if ui_text_to_select:
                        combo_box.setCurrentText(ui_text_to_select)
                    else:
                        combo_box.setCurrentIndex(0) # 如果找不到，默认选第一个

            # 加载其他配置 (保持不变)
            subject_widget = self.get_ui_element('subject_text', QComboBox)
            if subject_widget: subject_widget.setCurrentText(self.config_manager.subject)
            self.get_ui_element('cycle_number').setValue(self.config_manager.cycle_number)
            self.get_ui_element('wait_time').setValue(self.config_manager.wait_time)
            self.get_ui_element('dual_evaluation_enabled').setChecked(self.config_manager.dual_evaluation_enabled)
            self.get_ui_element('score_diff_threshold').setValue(self.config_manager.score_diff_threshold)
            
            # 加载题目配置 (单题模式只处理第一题)
            q_config = self.config_manager.get_question_config(1)
            std_answer = self.get_ui_element('StandardAnswer_text_1')
            if std_answer: std_answer.setPlainText(q_config.get('standard_answer', ''))

            # 加载完成后，应用所有UI约束
            self._apply_ui_constraints()
            self.log_message("配置已成功加载到UI并应用约束。")
            self._config_loaded_once = True
        except Exception as e:
            self.log_message(f"加载配置到UI时出错: {e}\n{traceback.format_exc()}", is_error=True)
        finally:
            self._is_initializing = False

    # ==========================================================================
    #  按钮点击和事件处理 (大部分保持不变)
    # ==========================================================================

    def auto_run_but_clicked(self):
        """自动运行按钮点击事件"""
        self.log_message("尝试在运行前保存所有配置...")
        if not self.config_manager.save_all_configs_to_file():
            self.log_message("错误：运行前保存配置失败！无法启动自动阅卷。", is_error=True)
            # 创建完整显示的保存失败提示框
            msg_box = QMessageBox(self)
            msg_box.setIcon(QMessageBox.Critical)
            msg_box.setWindowTitle("保存失败")
            msg_box.setText("保存配置失败。\n\n请检查下方日志以获取更多详细信息。")
            msg_box.setSizeGripEnabled(True)
            msg_box.setMinimumSize(500, 200)
            msg_box.setStyleSheet("QLabel{min-width: 400px;}")
            msg_box.setStandardButtons(QMessageBox.Ok)
            msg_box.exec_()
            return
        self.log_message("所有配置已成功保存。")
        
        # --- 核心修改: check_required_settings 现在直接使用 ConfigManager 的数据 ---
        if not self.check_required_settings():
            return # check_required_settings 内部会打日志和弹窗
        
        # ... 单题模式启动逻辑 ...
        try:
            # 单题模式只处理第一题
            enabled_questions_indices = [1]

            # 检查答案区域配置
            q_config = self.config_manager.get_question_config(1)
            if not q_config or 'answer_area' not in q_config or not q_config['answer_area']:
                # 创建完整显示的配置不完整提示框
                msg_box = QMessageBox(self)
                msg_box.setIcon(QMessageBox.Warning)
                msg_box.setWindowTitle("配置不完整")
                msg_box.setText("请先为第一题框定答案区域。\n\n第一题的答案区域配置缺失，请在题目配置对话框中设置答案区域坐标。")
                msg_box.setSizeGripEnabled(True)
                msg_box.setMinimumSize(500, 150)
                msg_box.setStyleSheet("QLabel{min-width: 400px;}")
                msg_box.setStandardButtons(QMessageBox.Ok)
                msg_box.exec_()
                return

            # 准备参数给 AutoThread
            dual_evaluation = self.config_manager.dual_evaluation_enabled

            question_configs_for_worker = []
            q_config = self.config_manager.get_question_config(1).copy()
            q_config['question_index'] = 1
            q_config['dual_eval_enabled'] = dual_evaluation
            question_configs_for_worker.append(q_config)

            params = {
                'cycle_number': self.config_manager.cycle_number,
                'wait_time': self.config_manager.wait_time,
                'question_configs': question_configs_for_worker,
                'dual_evaluation': dual_evaluation,
                'score_diff_threshold': self.config_manager.score_diff_threshold,
                'first_model_id': self.config_manager.first_modelID,
                'second_model_id': self.config_manager.second_modelID,
                'is_single_question_one_run': True
            }

            self.worker.set_parameters(**params)
            self.worker.start()
            self.update_ui_state(is_running=True)
            self.log_message(f"自动阅卷已启动: 循环 {params['cycle_number']} 次, 等待 {params['wait_time']} 秒")

        except Exception as e:
            self.log_message(f"启动自动阅卷出错: {e}", is_error=True)
            traceback.print_exc()

    def check_required_settings(self):
        """检查必要的设置是否已配置"""
        errors = []
        # 直接从 ConfigManager 检查
        if not self.config_manager.first_api_provider: errors.append("请为第一组API选择一个供应商")
        if not self.config_manager.first_api_key.strip(): errors.append("第一组API的密钥不能为空")
        if not self.config_manager.first_modelID.strip(): errors.append("第一组API的模型ID不能为空")

        if self.config_manager.dual_evaluation_enabled:
            if not self.config_manager.second_api_provider: errors.append("双评模式下，请为第二组API选择一个供应商")
            if not self.config_manager.second_api_key.strip(): errors.append("第二组API的密钥不能为空")
            if not self.config_manager.second_modelID.strip(): errors.append("第二组API的模型ID不能为空")

        # 单题模式只检查第一题
        q_cfg = self.config_manager.get_question_config(1)
        if not q_cfg.get('standard_answer', '').strip():
            errors.append("第一题未设置评分细则")

        if errors:
            # --- 优化错误提示 ---
            title = "启动前请完善配置"
            intro = "自动阅卷无法启动，因为缺少以下必要信息：\n"
            error_details = "\n".join([f"  - {e}" for e in errors])
            final_message = f"{intro}\n{error_details}\n\n请在主界面补充完整后再试。"

            # 创建完整显示的错误提示框
            msg_box = QMessageBox(self)
            msg_box.setIcon(QMessageBox.Warning)
            msg_box.setWindowTitle(title)
            msg_box.setText(final_message)
            msg_box.setSizeGripEnabled(True)
            msg_box.setMinimumSize(600, 300)
            msg_box.setStyleSheet("QLabel{min-width: 500px;}")
            msg_box.setStandardButtons(QMessageBox.Ok)
            msg_box.exec_()
            return False
        return True
    
    def test_api_connections(self):
        """测试API连接"""
        try:
            # 测试前无需手动更新，因为 ApiService 每次都会从 ConfigManager 获取最新配置
            self.log_message("正在测试第一组API连接...")
            success1, message1 = self.api_service.test_api_connection("first")
            
            dual_eval_checkbox = self.get_ui_element('dual_evaluation_enabled')
            is_dual_active_ui = dual_eval_checkbox.isChecked() and dual_eval_checkbox.isEnabled()

            result_message = ""
            if is_dual_active_ui:
                self.log_message("已开启双评模式，正在测试第二组API连接...")
                success2, message2 = self.api_service.test_api_connection("second")
                result_message = (
                    f"API测试结果:\n\n第一组API: {'✓ ' if success1 else '✗ '}{message1}\n\n"
                    f"第二组API: {'✓ ' if success2 else '✗ '}{message2}"
                )
                if success1 and success2: self.log_message("API测试完成：两个API均可正常使用")
                else: self.log_message("API测试完成：存在API无法正常使用", is_error=True)
            else:
                result_message = f"API测试结果:\n\n第一组API: {'✓ ' if success1 else '✗ '}{message1}"
                if success1: self.log_message("API测试完成：第一组API可正常使用")
                else: self.log_message("API测试完成：第一组API无法正常使用", is_error=True)

            # 创建完整显示的API测试结果提示框
            msg_box = QMessageBox(self)
            msg_box.setIcon(QMessageBox.Information)
            msg_box.setWindowTitle("API测试结果")
            msg_box.setText(f"API测试已完成。\n\n{result_message}")
            msg_box.setSizeGripEnabled(True)
            msg_box.setMinimumSize(500, 200)
            msg_box.setStyleSheet("QLabel{min-width: 400px;}")
            msg_box.setStandardButtons(QMessageBox.Ok)
            msg_box.exec_()
        except Exception as e:
            self.log_message(f"API测试出错: {str(e)}", is_error=True)
            # 创建完整显示的API测试错误提示框
            msg_box = QMessageBox(self)
            msg_box.setIcon(QMessageBox.Critical)
            msg_box.setWindowTitle("API测试错误")
            msg_box.setText(f"测试过程中发生错误。\n\n错误详情:\n{str(e)}")
            msg_box.setSizeGripEnabled(True)
            msg_box.setMinimumSize(500, 200)
            msg_box.setStyleSheet("QLabel{min-width: 400px;}")
            msg_box.setStandardButtons(QMessageBox.Ok)
            msg_box.exec_()

    def closeEvent(self, event):
        """窗口关闭事件（优化版）"""
        if self.worker.isRunning():
            self.worker.stop()
            self.worker.wait()  # 等待线程安全退出，这是一个好习惯

        # 遍历字典值的副本，因为我们不需要在循环中修改字典
        for window in list(self.answer_windows.values()):
            try:
                # 直接尝试关闭。
                # 1. 如果窗口还开着，它会被正常关闭。
                # 2. 如果窗口已经关闭但对象还存在，调用 close() 通常是无害的。
                # 3. 如果底层对象已被删除，这里会立即触发 RuntimeError。
                window.close()
            except RuntimeError:
                # 捕获到错误，说明这个窗口引用已经失效。
                # 我们什么都不用做，只需安静地忽略它即可。
                self.log_message("一个答案窗口在主窗口关闭前已被销毁，跳过关闭操作。")
                pass

        # 循环结束后，清空字典，确保所有（可能无效的）引用都被清除
        self.answer_windows.clear()

        # --- 保存配置的逻辑保持不变 ---
        self.log_message("尝试在关闭程序前保存所有配置...")
        if not self.config_manager.save_all_configs_to_file():
            self.log_message("警告：关闭程序前保存配置失败。", is_error=True)
        else:
            self.log_message("所有配置已在关闭前成功保存。")

        event.accept()

    # --- 以下是其他未发生重大逻辑变化的函数，保持原样 ---
    # ... (包括 on_dual_evaluation_changed, _apply_ui_constraints, on_worker_finished, get_ui_element, 等等) ...
    # ... 请将您原文件中的这些函数复制到这里 ...
    # 为了完整性，我将提供这些函数的简化版或完整版
    
    def on_dual_evaluation_changed(self, state):
        if self._is_initializing: return
        is_enabled = bool(state)
        self.handle_checkBox_save('dual_evaluation_enabled', is_enabled)
        self._apply_ui_constraints()

    def _is_single_q1_mode(self):
        """检查当前是否只启用了第一题"""
        for i in range(2, self.max_questions + 1):
            cb = self.get_ui_element(f'enableQuestion{i}')
            if cb and cb.isChecked():
                return False
        return True

    def _apply_ui_constraints(self):
        is_single_q1_mode = self._is_single_q1_mode()

        dual_eval_checkbox = self.get_ui_element('dual_evaluation_enabled')
        if dual_eval_checkbox:
            dual_eval_checkbox.setEnabled(is_single_q1_mode)
            if not is_single_q1_mode and dual_eval_checkbox.isChecked():
                dual_eval_checkbox.blockSignals(True)
                dual_eval_checkbox.setChecked(False)
                self.handle_checkBox_save('dual_evaluation_enabled', False)
                dual_eval_checkbox.blockSignals(False)
            
            is_dual_active = dual_eval_checkbox.isChecked() and dual_eval_checkbox.isEnabled()
            self.get_ui_element('score_diff_threshold').setEnabled(is_dual_active)
            self.get_ui_element('second_api_url').setEnabled(is_dual_active)
            self.get_ui_element('second_api_key').setEnabled(is_dual_active)
            self.get_ui_element('second_modelID').setEnabled(is_dual_active)

        q1_config = self.config_manager.get_question_config(1)
        is_q1_three_step_enabled = q1_config.get('enable_three_step_scoring', False)

        can_enable_next = True
        for i in range(2, self.max_questions + 1):
            cb_i = self.get_ui_element(f'enableQuestion{i}')
            if not cb_i: continue
            
            should_be_enabled = can_enable_next and not is_q1_three_step_enabled
            cb_i.setEnabled(should_be_enabled)
            
            if not should_be_enabled and cb_i.isChecked():
                cb_i.blockSignals(True)
                cb_i.setChecked(False)
                self.handle_checkBox_save(f'question_{i}_enabled', False)
                cb_i.blockSignals(False)
            
            self.update_config_button(i, cb_i.isChecked())
            can_enable_next = cb_i.isChecked()
    
    def on_question_enabled_changed(self, state):
        if self._is_initializing: return
        sender = self.sender()
        if not sender: return
        try:
            q_index = int(sender.objectName().replace('enableQuestion', ''))
            self.handle_checkBox_save(f"question_{q_index}_enabled", bool(state))
            self._apply_ui_constraints()
        except (ValueError, AttributeError): pass
        
    def update_config_button(self, question_index, is_enabled):
        btn = self.get_ui_element(f'configQuestion{question_index}')
        if btn: btn.setEnabled(is_enabled)
        
    def log_message(self, message, is_error=False, level=None):
        """
        显示日志消息，支持级别过滤。

        Args:
            message: 日志消息内容
            is_error: 是否为错误消息（向后兼容）
            level: 日志级别 (INFO, DETAIL, RESULT, ERROR)
        """
        # 自动确定级别（向后兼容）
        if level is None:
            level = self.LOG_LEVEL_ERROR if is_error else self.LOG_LEVEL_INFO

        # 日志过滤：只显示RESULT和ERROR级别的消息
        if level not in [self.LOG_LEVEL_RESULT, self.LOG_LEVEL_ERROR]:
            return

        log_widget = self.get_ui_element('log_text')
        if log_widget:
            if level == self.LOG_LEVEL_ERROR:
                color = "red"
                prefix = "[错误]"
            elif level == self.LOG_LEVEL_RESULT:
                color = "black"
                prefix = "[结果]"
            else:
                color = "blue"
                prefix = "[信息]"

            log_widget.append(f'<span style="color:{color}">{prefix} {message}</span>')

        # 控制台始终输出所有消息
        print(f"[{level}] {message}")

    def on_worker_finished(self):
        self.update_ui_state(is_running=False)
    
    def on_worker_error(self, error_message):
        self.log_message(f"任务中断: {error_message}", is_error=True)
        self.update_ui_state(is_running=False)
        
    def update_ui_state(self, is_running):
        self.get_ui_element('auto_run_but').setEnabled(not is_running)
        self.get_ui_element('stop_but').setEnabled(is_running)
        
        # 禁用所有配置相关控件
        config_controls = [
            'first_api_url', 'first_api_key', 'first_modelID',
            'second_api_url', 'second_api_key', 'second_modelID',
            'dual_evaluation_enabled', 'score_diff_threshold', 'subject_text',
            'cycle_number', 'wait_time', 'api_test_button'
        ]
        for i in range(1, 5):
            config_controls.append(f'configQuestion{i}')
            config_controls.append(f'StandardAnswer_text_{i}')
            if i > 1: config_controls.append(f'enableQuestion{i}')

        for name in config_controls:
            widget = self.get_ui_element(name)
            if widget:
                widget.setEnabled(not is_running)

        if is_running:
            if not self.isMinimized(): self.showMinimized()
        else:
            if self.isMinimized(): self.showNormal(); self.activateWindow()
            self._apply_ui_constraints() # 任务结束后恢复UI约束

    def stop_auto_thread(self):
        if self.worker.isRunning():
            self.worker.stop()
            self.log_message("已发送停止请求至自动阅卷线程。")
        else:
            self.update_ui_state(is_running=False) # 确保UI状态正确

    # ... 其他如 get_ui_element, open_question_config_dialog 等函数保持原样 ...
    # 您可以将原文件中的这些函数直接复制过来
    def get_ui_element(self, element_name, element_type=None):
        if element_name in self._ui_cache:
            return self._ui_cache[element_name]
        
        element = self.findChild(QWidget, element_name)
        if element:
            self._ui_cache[element_name] = element
        return element
        
    def open_question_config_dialog(self, question_index):
        # 延迟导入以避免循环依赖
        from .question_config_dialog import QuestionConfigDialog

        dialog = QuestionConfigDialog(
            parent=self,
            config_manager=self.config_manager,
            question_index=question_index,
            is_single_q1_mode_active=self._is_single_q1_mode()
        )
        if dialog.exec_() == QDialog.Accepted:
            self.load_config_to_ui()

    def get_or_create_answer_window(self, question_index):
        from .question_config_dialog import MyWindow2
        if question_index not in self.answer_windows:
            window = MyWindow2(parent=self, question_index=question_index)
            # 连接窗口关闭信号，用于清理字典
            window.status_changed.connect(
                lambda status, q_idx=question_index: self._on_answer_window_status_changed(q_idx, status)
            )
            self.answer_windows[question_index] = window
        return self.answer_windows[question_index]

    def _on_answer_window_status_changed(self, question_index, status):
        """处理答案框窗口状态变化"""
        if status == "closed":
            # 当窗口关闭时，从字典中移除引用
            if question_index in self.answer_windows:
                self.log_message(f"第{question_index}题答案框窗口已关闭，从字典中移除引用")
                del self.answer_windows[question_index]
    
    # ... 您原有的其他辅助函数，如 connect_signals, setup_* 系列函数 ...
    # 这些函数的内部逻辑基本不需要大改，因为它们大多是连接信号或设置简单的UI属性
    # 我在这里提供简化版，您可以与您的版本对比
    def connect_signals(self):
        # 这个函数在 main.py 中被调用，这里留空，因为连接逻辑移到了 main.py 的 Application 类中
        pass

    def setup_question_selector(self):
        # from PyQt5.QtWidgets import QButtonGroup
        # self.question_button_group = QButtonGroup(self)
        # self.question_button_group.buttonClicked.connect(self.on_question_changed)
        pass # 假设UI文件已自动连接

    def on_question_changed(self, button): pass

    def setup_text_fields(self):
        for i in range(1, 5):
            widget = self.get_ui_element(f'StandardAnswer_text_{i}')
            if widget: widget.setPlaceholderText(f"请输入第{i}题的评分细则...")

        # 设置评分细则和日志的字体大小为12pt
        font = QFont("微软雅黑", 12)
        standard_answer_widget = self.get_ui_element('StandardAnswer_text_1')
        if standard_answer_widget:
            standard_answer_widget.setFont(font)
        log_widget = self.get_ui_element('log_text')
        if log_widget:
            log_widget.setFont(font)

    def setup_dual_evaluation(self):
        cb = self.get_ui_element('dual_evaluation_enabled')
        if cb: cb.stateChanged.connect(self.on_dual_evaluation_changed)
        spin = self.get_ui_element('score_diff_threshold')
        if spin: spin.valueChanged.connect(lambda val: self.handle_spinBox_save('score_diff_threshold', val))

    def on_subject_changed(self, index):
        # 此函数在我的重构中未直接使用，但如果您需要它，可以这样实现
        combo = self.sender()
        if combo: self.handle_comboBox_save('subject', combo.currentText())

    def _connect_signals(self):
        """统一连接所有UI控件的信号与槽"""
        # 连接按钮点击
        self.get_ui_element('auto_run_but').clicked.connect(self.auto_run_but_clicked)
        self.get_ui_element('stop_but').clicked.connect(self.stop_auto_thread)
        self.get_ui_element('api_test_button').clicked.connect(self.test_api_connections)
        for i in range(1, self.max_questions + 1):
            btn = self.get_ui_element(f'configQuestion{i}')
            if btn:
                btn.clicked.connect(lambda checked, q=i: self.open_question_config_dialog(q))

        # 连接即时保存信号
        self._connect_direct_edit_save_signals()

        # 连接题目启用复选框
        for i in range(2, self.max_questions + 1):
            checkbox = self.get_ui_element(f'enableQuestion{i}')
            if checkbox:
                checkbox.stateChanged.connect(self.on_question_enabled_changed)

    # 缓存合并相关方法
    def update_cache_status(self, message):
        """更新缓存状态显示"""
        if hasattr(self, 'cache_status_label') and self.cache_status_label:
            self.cache_status_label.setText(message)

    def show_merge_button(self, show):
        """显示或隐藏合并按钮"""
        if hasattr(self, 'cache_status_label') and self.cache_status_label:
            self.cache_status_label.setVisible(show)
        if hasattr(self, 'merge_cache_button') and self.merge_cache_button:
            self.merge_cache_button.setVisible(show)
            self.merge_cache_button.setEnabled(show)

    def request_merge_cache(self):
        """请求合并缓存记录"""
        # 通过信号发送到Application类
        # 假设Application类有连接的槽
        # 这里发出一个信号或者直接调用
        # 由于MainWindow不知道Application实例，我们需要通过信号
        # 临时：使用一个自定义信号
        from PyQt5.QtCore import pyqtSignal
        if not hasattr(self, 'merge_requested_signal'):
            self.merge_requested_signal = pyqtSignal()
            # 在Application中连接: self.main_window.merge_requested_signal.connect(self.manual_merge_records)
        self.merge_requested_signal.emit()

# --- END OF FILE main_window.py ---
