import sys
import re
import json

from typing import List, Dict, Union, Callable, Any

sys.dont_write_bytecode = True
from ..base import Task
from ..vm import VTask

from ..base.utils import getitem, error_factory
from .chimerax import RawManager, VMManager


class TaskMixin:
    def __init__(self) -> None:
        # this class is not independent: manager, evaluate, vlog needed
        raise

    @Task._config_handler
    def check_config(self, eval_item) -> None:
        assert eval_item["type"] in ("info", "states", "file")
        assert "key" in eval_item
        if eval_item["type"] == "info":
            assert "value" in eval_item
            assert isinstance(eval_item["value"], list)
            for sub_item in eval_item["value"]:
                assert isinstance(sub_item, str)
        elif eval_item["type"] == "states":
            assert "value" in eval_item or "pattern" in eval_item
        elif eval_item["type"] == "file":
            assert "value" in eval_item

    def _destroy(self: Union["RawTask", "VMTask"]) -> bool:
        _, code = self.manager._call(f"destroy")
        return code

    def _open(self: Union["RawTask", "VMTask"], name: str) -> bool:
        _, code = self.manager._call(f"open {name}")
        return code

    def _turn(self: Union["RawTask", "VMTask"], axis: str, angle: int) -> bool:
        _, code = self.manager._call(f"turn {axis} {angle}")
        return code

    def _alphafold_match(self: Union["RawTask", "VMTask"], name: str) -> bool:
        _, code = self.manager._call(f"alphafold match {name}")
        return code

    def _color(self: Union["RawTask", "VMTask"], style: str) -> bool:
        command = f"color {style}" if style != "rainbow" else style
        _, code = self.manager._call(command)
        return code

    def _clear_log(self: Union["RawTask", "VMTask"]) -> bool:
        _, code = self.manager._call(f"log clear")
        return code

    @error_factory(False)
    def _eval_states(
        self: Union["RawTask", "VMTask"],
        eval_item: Dict[str, Any],
        current_states: Dict[str, Any]
    ) -> bool:
        find: str = getitem(eval_item, "find", None)
        key: str = eval_item["key"]
        value: str = getitem(eval_item, "value", None)
        pattern: str = getitem(eval_item, "pattern", None)

        # if value is set to null
        # check the inexistence of item
        check_null = "value" in eval_item and eval_item["value"] is None

        raw_key = None
        if key.startswith("lambda"):
            key: Callable[[str], Union[str, bool]] = eval(key)

        # find meta_key by key-value pair using find(key)
        # process meta_key to raw_key using key(meta_key)
        # type of find: str -> Any -> bool
        # type of key: str -> str
        if find is not None:
            find: Callable[[str, Any], bool] = eval(find)
            meta_keys: List[str] = [
                key for key, value in current_states.items()
                if find(key, value)
            ]
            if check_null:
                return len(raw_keys) == 0
            raw_key: str = key(meta_keys[0])

        # find raw_key directly using key(key)
        # type of key: str -> bool
        elif hasattr(key, "__call__"):
            raw_keys: List[str] = list(filter(key, current_states.keys()))
            if check_null:
                return len(raw_keys) == 0
            raw_key: str = raw_keys[0]

        # key itself is raw_key
        # type of key: str
        else:
            if check_null:
                return raw_key not in current_states
            raw_key: str = key

        # get targeted raw_value by raw_key
        raw_value: Any = current_states[raw_key]

        # cast to str first
        if pattern is not None:
            if not isinstance(raw_value, str):
                raw_value: str = json.dumps(raw_value)
            return re.search(pattern, raw_value) is not None
        # extract match
        # no assumptions for value type
        elif value is not None:
            return value == raw_value
        return False

    # prerequisite of calling TaskMixin._eval_info:
    # - task.manager._call()
    @error_factory(False)
    def _eval_info(
        self: Union["RawTask", "VMTask"],
        eval_item: Dict[str, Any],
        _: Dict[str, Any]
    ) -> bool:
        key = eval_item["key"]
        value = eval_item["value"]

        log_message, _ = self.manager._call(f"info {key}")
        nested_logs = [log.strip().split("\n") for log in log_message]
        info_list = [log for logs in nested_logs for log in logs if log != ""]
        return set(info_list) == set(value)

    @error_factory(False)
    def _eval_file(
        self: Union["RawTask", "VMTask"],
        eval_item: Dict[str, Any],
        _: Dict[str, Any]
    ) -> bool:
        key = eval_item["key"]
        value = eval_item["value"]

        content = self.manager.read_file(key)
        if value.startswith("lambda"):
            value = eval(value)()
        return content is not None and value in content

    # prerequisite of calling TaskMixin.eval:
    # - task.evaluate
    # - task.manager.status_dump()
    def eval(self: Union["RawTask", "VMTask"]) -> bool:
        current_states = self.manager.states_dump()
        for eval_item in self.evaluate:
            eval_type = eval_item["type"]
            # eval_func is not bound method because of the decorator factory?
            eval_func = getattr(self, f"_eval_{eval_type}")
            if not eval_func(eval_item, current_states):
                self.vlog.info(f"Evaluation failed at {eval_type} of {eval_item['key']}.")
                return False
        return True


class RawTask(Task, TaskMixin):
    def __init__(
        self,
        config_path: str,
        manager: RawManager,
        *args,
        **kwargs
    ) -> None:
        # to enable Pylance type checker
        assert isinstance(manager, RawManager)
        self.manager = manager

        super().__init__(config_path, manager, *args, **kwargs)
        self.check_config()

    def _init(self) -> bool:
        _, code = self.manager._call("close")
        return code and self.manager.clear_history()

    @Task._stop_handler
    def eval(self) -> bool:
        # MRO: RawTask -> Task -> TaskMixin -> object
        return super(Task, self).eval()


class VMTask(VTask, TaskMixin):
    def __init__(
        self,
        config_path: str,
        manager: VMManager,
        *args,
        **kwargs
    ) -> None:
        # to enable Pylance type checker
        assert isinstance(manager, VMManager)
        self.manager = manager

        super().__init__(config_path, manager, *args, **kwargs)
        self.check_config()

    @Task._stop_handler
    def eval(self) -> bool:
        # MRO: VMTask -> VTask -> Task -> TaskMixin -> object
        return super(Task, self).eval()
