"""补查卖家精灵包装，回填表格里已经写出但长宽高为空的行。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from seller_auto.browser import ArgentinaBrowser
from seller_auto.browser_launch import snapshot_profile
from seller_auto.exit_identity import china_identity
from seller_auto.lingdong import apply_last_mile, collect_market_quotes, quote_jobs
from seller_auto.matches import known_orders_fields
from seller_auto.pacing import DelaySettings
from seller_auto.sellersprite import open_1688_from_row, open_competitor
from seller_auto.sourcing import read_page_source
from seller_auto.step_log import step
from seller_auto.storage import atomic_write_json
from seller_auto.workbook import fill_package_rows, seller_workbook_path, sheet_entries, template_path


def package_incomplete(package: Any) -> bool:
    """重量或长宽高缺任意一项，都要再查。空包装也算缺。"""

    if not isinstance(package, dict) or not package:
        return True
    if package.get("weight") in (None, ""):
        return True
    return any(package.get(key) in (None, "") for key in ("length", "width"))


def missing_package_ids(state: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for product_id in list(state.get("product_order") or []):
        product = (state.get("products") or {}).get(product_id) or {}
        if not sheet_entries(product):
            continue
        if not package_incomplete(product.get("package_size")):
            continue
        ids.append(product_id)
    return ids


def _merge_package(current: Any, fetched: Any) -> Any:
    if not isinstance(fetched, dict) or not fetched:
        return current
    if not isinstance(current, dict) or not current:
        return dict(fetched)
    merged = dict(current)
    for key, value in fetched.items():
        if merged.get(key) in (None, "") and value not in (None, ""):
            merged[key] = value
    return merged


def _has_source(product: dict[str, Any]) -> bool:
    source = product.get("source_price") or {}
    return isinstance(source, dict) and source.get("average") not in (None, "")


def _query_one(browser: ArgentinaBrowser, product_id: str) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    package, page = open_competitor(browser, product_id)
    source = None
    if page is not None:
        opened = open_1688_from_row(browser, page, product_id)
        if opened is not None:
            source = read_page_source(browser, opened)
    return package, source


def backfill_packages(
    state: dict[str, Any],
    *,
    workbook_path: Path,
    settings: DelaySettings,
    profile_dir: Path,
    ids: list[str] | None = None,
    query: Any = None,
    quote: Any = None,
    write_rows: Any = None,
) -> dict[str, Any]:
    targets = list(ids if ids is not None else missing_package_ids(state))
    result = {"ids": targets, "updated": 0, "quoted": 0}
    if not targets:
        return result
    browser = None
    if query is None or quote is None:
        # 店铺进程独占 profile-enrich。回填再开同一目录会把那只 Chrome 顶掉，之后每条包装都读空。
        launch_dir = snapshot_profile(
            Path(str(profile_dir) + "-enrich"),
            Path(str(profile_dir) + "-enrich-backfill"),
        )
        step("资料", "回填使用独立浏览器配置，避免关掉正在查询的卖家精灵")
        identity = china_identity()
        browser = ArgentinaBrowser(
            settings,
            launch_dir,
            headless=settings.headless,
            locale=identity.locale,
            accept_language=identity.accept_language,
            timezone_id=identity.timezone_id,
            geolocation=identity.geolocation,
            use_proxy=False,
        )
        browser.start()
    try:
        for product_id in targets:
            product = state["products"][product_id]
            keep_source = _has_source(product)
            if query is None:
                assert browser is not None
                if keep_source:
                    package, _page = open_competitor(browser, product_id)
                    source = None
                else:
                    package, source = _query_one(browser, product_id)
                try:
                    browser.close_detail()
                except Exception:
                    pass
            else:
                package, source = query(product_id)
                if keep_source:
                    source = None
            product["package_size"] = _merge_package(product.get("package_size"), package)
            if source:
                product["source_price"] = source
            apply_last_mile(product)
            if package or source:
                step("资料", f"{product_id} 包装回填完成")
            else:
                step("资料", f"{product_id} 回填仍没有读到包装和货源")
        jobs = []
        for product_id in targets:
            product = state["products"][product_id]
            jobs.extend(quote_jobs(product_id, product))
        if jobs:
            if quote is None:
                assert browser is not None
                quotes = collect_market_quotes(
                    browser,
                    [(job["links"], job.get("package")) for job in jobs],
                )
            else:
                quotes = quote(jobs)
            for job, item in zip(jobs, quotes):
                product = state["products"][job["product_id"]]
                if item.get("orders") is not None:
                    field = str(job.get("orders_field") or "market_orders")
                    if field not in known_orders_fields():
                        field = "market_orders"
                    product[field] = item["orders"]
                    result["quoted"] += 1
        products = [
            state["products"][product_id]
            for product_id in targets
            if (state["products"][product_id] or {}).get("workbook_written")
        ]
        if write_rows is None:
            result["updated"] = fill_package_rows(workbook_path, products, replace_fba=True) if products else 0
        else:
            result["updated"] = write_rows(workbook_path, products) if products else 0
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
    return result


def run(state_path: Path, workbook: Path | None = None) -> dict[str, Any]:
    settings = DelaySettings.from_argv()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    seller_id = str(state.get("seller_id") or "")
    default_workbook = seller_workbook_path(seller_id) if seller_id else template_path()
    result = backfill_packages(
        state,
        workbook_path=workbook or default_workbook,
        settings=settings,
        profile_dir=Path(settings.profile_dir),
    )
    atomic_write_json(state_path, state)
    return result


if __name__ == "__main__":
    path = Path("output/AY1LKQUJT046T/20261027-183640-a40b89ed/state.json")
    print(json.dumps(run(path), ensure_ascii=False))
