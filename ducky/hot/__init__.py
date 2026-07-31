"""ducky.hot — HOT 主链路路由子包（v9.1 全套整洁）"""
from ducky.hot.add import register_add_routes
from ducky.hot.crud import register_crud_routes
from ducky.hot.health import register_health_routes
from ducky.hot.search import register_search_routes


def register_core_routes(app) -> None:
    register_health_routes(app)
    register_add_routes(app)
    register_search_routes(app)
    register_crud_routes(app)

__all__ = ["register_core_routes"]
