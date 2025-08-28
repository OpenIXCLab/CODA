import copy
import sys
import functools
import string
import os
from typing import Optional, List, Tuple, Dict
from typing import Callable, Any, Set, FrozenSet

from PIL import Image
from requests import Response
import time

sys.dont_write_bytecode = True
from . import utils
from .manager import OBS, Manager
from .log import VirtualLog
from .model import Content, TextContent, ImageContent
from .model import Message, Model
from .utils import TypeSort
from .prompt import CodeLike, Primitive
from .prompt import AIOPromptFactory
from .prompt import PlannerPromptFactory
from .prompt import GrounderPromptFactory


class Overflow:
    @staticmethod
    @utils.error_factory(False)
    def openai_gpt(response: Response) -> bool:
        return response.json()["error"]["code"] == "context_length_exceeded"

    @staticmethod
    @utils.error_factory(False)
    def openai_lmdeploy(response: Response) -> bool:
        return Model._access_openai(response).content[0].text == ""

    @staticmethod
    @utils.error_factory(False)
    def openai_siliconflow(response: Response) -> bool:
        return response.json()["code"] == 20015

    @staticmethod
    @utils.error_factory(False)
    def openai_newapi(response: Response) -> bool:
        return response.json()["error"]["message"].startswith("The input token count")

    @staticmethod
    @utils.error_factory(False)
    def anthropic(response: Response) -> bool:
        return response.json()["error"]["type"] == "request_too_large"


class Agent:
    def __init__(
        self,
        model: Model,
        overflow_style: Optional[str] = None,
        context_window: int = 15, # support longer
        hide_text: bool = False,
        code_style: str = "antiquot"
    ) -> None:
        assert isinstance(model, Model)
        self.model = model

        assert overflow_style is None or hasattr(Overflow, overflow_style)
        self.overflow_style = overflow_style
        self.overflow_handler: Optional[Callable[[Response], bool]] = None \
            if overflow_style is None \
            else getattr(Overflow, overflow_style)

        assert isinstance(context_window, int)
        assert context_window >= 0
        self.context_window = context_window

        assert isinstance(hide_text, bool)
        self.hide_text = hide_text

        assert hasattr(CodeLike, handler_name:=f"extract_{code_style}")
        self.code_style = code_style
        self.code_handler: Callable[
            [Content, Set[str], List[List[int]]],
            List[CodeLike]
        ] = getattr(CodeLike, handler_name)

        self.vlog = VirtualLog()

    def _init(self, inst: str) -> None:
        self.system_message: Message = self.model.message(
            role="system",
            content=[TextContent(inst.strip())]
        )
        self.context: List[Message] = []

    @staticmethod
    def _init_handler(method: Callable) -> Callable:
        @functools.wraps(method)
        def _init_wrapper(
            self,
            obs: Dict[str, Any],
            init: Optional[Dict] = None
        ) -> List[Content]:
            if init is not None:
                self._init(obs_keys=frozenset(obs.keys()), **init)
            return method(self, obs)
        return _init_wrapper

    # crucial: obs here may not be the same as in Task
    # e.g. Task.obs_types=SoM -> AIOAgent._step(obs={SoM, A11yTree})
    def _step(self, obs: Dict[str, Any], init: Optional[Dict] = None) -> None:
        raise NotImplementedError

    def __dump(self, context_count: int) -> List[Message]:
        return [
            self.system_message,
            *self.context[-context_count:]
        ]

    def dump_payload(self, context_length: int) -> Dict:
        if os.getenv("QWEN_PLANNER", '0') == "1":
            payload = self._Agent__dump(31)
            payload = copy.deepcopy(payload)
            for i in reversed(range(len(payload) - 4)):
                if payload[i].role == 'user':
                    del payload[i]
            if payload[-1].role == 'user':
                task = self.system_message.content[0].text.split('User Instruction\n')[1]
                payload[-1].content.append(TextContent('Task: ' + task))
        else:
            payload = self.__dump(context_length * 2 + 1)
        if os.getenv("NO_CONTEXT_IMAGE", "0") == "1":
            for i in reversed(range(len(payload) - 1)):
                if payload[i].role == 'user':
                    del payload[i]
        return [message._asdict(hide_text=(
            not index + 1 == len(payload) and self.hide_text
        )) for index, message in enumerate(payload)]

    def dump_history(self, hide: bool) -> Tuple[Dict, Dict]:
        return [
            message._asdict(show_context=True, hide_text=hide, hide_image=hide)
            for message in self.__dump(len(self.context))
        ]

    def __call__(
        self,
        contents: List[Content],
        shorten: int = 0,
        retry: int = 3,
        timeout: int = Manager.HETERO_TIMEOUT
    ) -> Message:
        assert hasattr(self, "context"), "Call _init() first"
        assert retry > 0, f"Max reties exceeded when calling {self.model.model_name}"

        assert isinstance(contents, list)
        for content in contents:
            assert isinstance(content, Content)

        context_length = self.context_window - shorten
        assert context_length >= 0, "Error when calculating context length"

        self.context.append(self.model.message(role="user", content=contents))
        while True:
            try:
                payload = self.dump_payload(context_length)
                response = self.model(payload, timeout)
                response.json()
                break
            except:
                self.vlog.info(f"message model fail\n" + response.text)
                time.sleep(1)

        is_overflow = False if self.overflow_handler is None \
            else self.overflow_handler(response)

        if is_overflow and context_length > 0:
            self.vlog.error(
                f"Overflow detected when requesting {self.model.model_name}; "
                f"set context_window={context_length - 1}."
            )
            return self(self.context.pop().content, shorten + 1, retry, timeout)
        assert not is_overflow, f"Unsolvable overflow when requesting {self.model.model_name}"

        response_message = self.model.access(response, context_length)
        if response_message is None:
            self.vlog.error(
                f"Unexpected error when requesting {self.model.model_name}.\n"
                    + response.text
            )
            Manager.pause(Primitive.WAIT_TIME)
            return self(self.context.pop().content, shorten, retry - 1, timeout)

        self.context.append(response_message)
        return response_message


class AIOAgent(Agent):
    USER_FLATTERY = "What's the next step that you will do to help with the task?"
    USER_OPENING: Dict[FrozenSet[str], str] = {
        frozenset({OBS.textual}): "Given the textual information as below:\n{textual}\n",
        frozenset({OBS.screenshot}): "Given the screenshot as below. ",
        frozenset({OBS.a11y_tree}): "Given the info from accessibility tree as below:\n{a11y_tree}\n",
        frozenset({OBS.a11y_tree, OBS.set_of_marks}): "Given the tagged screenshot and info from accessibility tree as below:\n{a11y_tree}\n",
        frozenset({OBS.screenshot, OBS.a11y_tree}): "Given the screenshot and info from accessibility tree as below:\n{a11y_tree}\n"
    }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.prompt_factory = AIOPromptFactory(self.code_style)

    def _init(
        self,
        obs_keys: FrozenSet[str],
        inst: str,
        type_sort: Optional[TypeSort] = None,
    ) -> None:
        system_inst = self.prompt_factory(obs_keys, type_sort)
        super()._init(system_inst(inst))

    @Agent._init_handler
    def _step(self, obs: Dict[str, Any]) -> List[Content]:
        formatter = string.Formatter()
        obs_keys = frozenset(obs.keys())

        opening = self.USER_OPENING[obs_keys]
        slots = [key for _, key, _, _ in formatter.parse(opening) if key]

        contents = [
            TextContent(opening + self.USER_FLATTERY + (" Only output your pyautogui code surrounded by ```." if os.getenv("EXAMPLE_7B", "0") == "1" else ""), {
                slot: utils.getitem(obs, slot, None)
                for slot in slots
            })
        ]

        images = [
            item for _, item in obs.items()
            if isinstance(item, Image.Image)
        ]
        contents += [ImageContent(image) for image in images]
        return contents


class PlannerAgent(AIOAgent):
    USER_OPENING: Dict[FrozenSet[str], str] = {
        frozenset({}): "",
        frozenset({OBS.textual}): "Given the textual information as below:\n{textual}\n",
        frozenset({OBS.screenshot}): "Given the screenshot as below. ",
        frozenset({OBS.set_of_marks}): "Given the tagged screenshot as below. "
    }

    # make sure that `code_style` is captured before kwargs
    def __init__(self, *args, code_style: str = "planner", **kwargs) -> None:
        super(AIOAgent, self).__init__(*args, code_style="planner", **kwargs)
        self.prompt_factory = PlannerPromptFactory(self.code_style)

    @Agent._init_handler
    def _step(self, obs: Dict[str, Any]) -> List[Content]:
        new_obs = obs.copy()
        if OBS.a11y_tree in new_obs:
            del new_obs[OBS.a11y_tree]
        return super()._step.__wrapped__(self, new_obs)


class GrounderAgent(AIOAgent):
    USER_OPENING: Dict[FrozenSet[str], str] = {
        frozenset({OBS.textual, OBS.schedule}): f"Given the textual information as below:\n{{textual}}\nand given the schedule from the planner:\n{{{OBS.schedule}}}\n",
        frozenset({OBS.screenshot, OBS.schedule}): f"Given the screenshot and the schedule from the planner as below:\n{{{OBS.schedule}}}\n",
        frozenset({OBS.a11y_tree, OBS.schedule}): f"Given the info from accessibility tree as below:\n{{a11y_tree}}\nand given the schedule from the planner:\n{{{OBS.schedule}}}\n",
        frozenset({OBS.a11y_tree, OBS.set_of_marks, OBS.schedule}): f"Given the tagged screenshot and info from accessibility tree as below:\n{{a11y_tree}}\nand given the schedule from the planner:\n{{{OBS.schedule}}}\n",
        frozenset({OBS.screenshot, OBS.a11y_tree, OBS.schedule}): f"Given the screenshot and info from accessibility tree as below:\n{{a11y_tree}}\nand given the schedule from the planner:\n{{{OBS.schedule}}}\n"
    }

    # make sure that `context_window` is captured before kwargs
    def __init__(self, *args, context_window: int = 0, **kwargs) -> None:
        super(AIOAgent, self).__init__(*args, context_window=0, **kwargs)
        self.prompt_factory = GrounderPromptFactory(self.code_style)
