from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class AIOSPlugin(ABC):
    """
    Abstract AI OS Plugin Interface.
    Extensions implement this interface to add new capabilities without slowing core runtime.
    """

    @property
    @abstractmethod
    def plugin_name(self) -> str:
        pass

    @property
    @abstractmethod
    def version(self) -> str:
        pass

    @abstractmethod
    def initialize(self) -> bool:
        pass

    @abstractmethod
    def execute(self, action: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        pass


class AIOSPluginManager:
    """
    Plugin-Based Architecture & Extension Registry.
    Dynamic plugin registration engine allowing custom capabilities to be added with zero impact on core runtime performance.
    """

    def __init__(self) -> None:
        self.plugins: Dict[str, AIOSPlugin] = {}

    def register_plugin(self, plugin: AIOSPlugin) -> bool:
        try:
            if plugin.initialize():
                self.plugins[plugin.plugin_name.lower()] = plugin
                return True
            return False
        except Exception:
            return False

    def unregister_plugin(self, plugin_name: str) -> bool:
        key = plugin_name.lower()
        if key in self.plugins:
            del self.plugins[key]
            return True
        return False

    def execute_plugin_action(self, plugin_name: str, action: str, parameters: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        key = plugin_name.lower()
        if key in self.plugins:
            return self.plugins[key].execute(action, parameters)
        return None

    def list_plugins(self) -> List[Dict[str, str]]:
        return [{"name": p.plugin_name, "version": p.version} for p in self.plugins.values()]
