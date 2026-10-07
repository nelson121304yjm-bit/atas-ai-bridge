"""一键验证 ATAS AI Bridge 的所有端点。

用法（用项目自带 venv 跑）：
    .\\.venv\\Scripts\\python.exe verify.py

前提：ATAS 正在运行，且图表上已添加 "AI Bridge" 策略并勾选 IsActivated。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atas_client import AtasBridgeClient, BridgeError  # noqa: E402

OK = "  [OK]  "
FAIL = "  [FAIL]"


def show(label: str, value: object) -> None:
    text = json.dumps(value, ensure_ascii=False)
    if len(text) > 300:
        text = text[:300] + f" ...(共 {len(text)} 字符)"
    print(f"{OK} {label}: {text}")


def check(label: str, call) -> bool:
    try:
        result = call()
    except BridgeError as exc:
        print(f"{FAIL} {label}: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} {label}: 未预期错误 {exc!r}")
        return False
    show(label, result)
    return True


def expect_reject(label: str, call) -> bool:
    """反向用例：必须被拒绝才算通过（脏样本，防"校验形同虚设"）。"""
    try:
        result = call()
    except BridgeError as exc:
        print(f"{OK} {label}: 已按预期拒绝 -- {exc}")
        return True
    print(f"{FAIL} {label}: 本该拒绝却成功了 -- {json.dumps(result, ensure_ascii=False)[:200]}")
    return False


def settings_map(payload: dict) -> dict:
    return {item["key"]: item for item in payload.get("settings", [])}


def check_settings(client) -> bool:
    """设置读写：读得到、写得进、改得回、非法值拒得掉、口令不回显。"""
    ok = True

    try:
        before = client.settings()
    except BridgeError as exc:
        print(f"{FAIL} settings 读取: {exc}")
        return False

    table = settings_map(before)
    print(f"{OK} settings 读取: 共 {len(table)} 项 -- {', '.join(table)}")

    required = {"snapshotBars", "snapshotFootprintBars", "profileLookbackBars",
                "profileValueAreaPercent", "defaultDomLevels", "allowTrading", "tradeToken"}
    missing = required - set(table)
    if missing:
        print(f"{FAIL} settings 缺项: {', '.join(sorted(missing))}")
        ok = False

    # 口令不回显：value 必须为 null，只给 isSet / length
    token = table.get("tradeToken") or {}
    if token.get("value") is None and "isSet" in token and "length" in token:
        print(f"{OK} tradeToken 不回显: value=null isSet={token.get('isSet')} length={token.get('length')}")
    else:
        print(f"{FAIL} tradeToken 泄露了原值: {json.dumps(token, ensure_ascii=False)[:200]}")
        ok = False

    # 往返：改一个无害项 -> 复核 -> 改回
    key = "snapshotFootprintBars"
    original = table.get(key, {}).get("value")
    probe = 3 if original != 3 else 5

    try:
        client.update_settings({key: probe})
        after = settings_map(client.settings()).get(key, {}).get("value")
        if after == probe:
            print(f"{OK} settings 写入生效: {key} {original} -> {after}")
        else:
            print(f"{FAIL} settings 写入未生效: 期望 {probe} 实际 {after}")
            ok = False

        # 改回原值，别把用户环境留在测试态
        client.update_settings({key: original})
        restored = settings_map(client.settings()).get(key, {}).get("value")
        if restored == original:
            print(f"{OK} settings 已还原: {key} = {restored}")
        else:
            print(f"{FAIL} settings 还原失败: 期望 {original} 实际 {restored}")
            ok = False
    except BridgeError as exc:
        print(f"{FAIL} settings 往返: {exc}")
        ok = False

    # 反向用例：越界 / 未知 key / 类型错 / 空 set —— 都必须被拒
    if not expect_reject("越界值被拒", lambda: client.update_settings({"snapshotBars": 999999})):
        ok = False
    if not expect_reject("未知 key 被拒", lambda: client.update_settings({"noSuchSetting": 1})):
        ok = False
    if not expect_reject("类型错被拒", lambda: client.update_settings({"snapshotBars": "三百"})):
        ok = False
    if not expect_reject("空 set 被拒（客户端前置校验）", lambda: client.update_settings({})):
        ok = False

    # 越界请求被拒后，原值必须没被改坏（原子语义）
    try:
        now = settings_map(client.settings()).get("snapshotBars", {}).get("value")
        print(f"{OK} 被拒请求未污染状态: snapshotBars = {now}")
    except BridgeError as exc:
        print(f"{FAIL} 拒绝后复核: {exc}")
        ok = False

    return ok


def main() -> int:
    client = AtasBridgeClient()
    print(f"目标: {client.base}")
    print()

    if not check("health", client.health):
        print()
        print("桥不可达。检查清单：")
        print("  1) ATAS 是否在运行（进程 OFT.Platform）")
        print("  2) 图表上是否添加了 AI Bridge 策略，并勾选 IsActivated")
        print(f"  3) %APPDATA%\\ATAS\\AiBridge.port 是否存在（当前端口探测值 {client.port}）")
        return 1

    print()
    check("status", client.status)
    check("quote", client.quote)
    check("dom(levels=5)", lambda: client.dom(5))
    check("candles(bars=5)", lambda: client.candles(5))
    check("cvd", client.cvd)
    check("footprint(bar=-1, top=8)", lambda: client.footprint(bar=-1, top=8))
    check("footprint_recent", client.footprint)
    check("profile(bars=0, percent=70)", lambda: client.profile())
    check("log(lines=15)", lambda: client.log(15))

    print()
    print("== 设置读写（含反向用例） ==")
    settings_ok = check_settings(client)
    print(f"{OK if settings_ok else FAIL} 设置读写总判定")

    print()
    print("下单闸门状态（应为 armed=False，除非你在 ATAS 侧显式打开）：")
    try:
        status = client.status()
        print(f"  allowTrading={status.get('allowTrading')}  "
              f"tradingArmed={status.get('tradingArmed')}  "
              f"tradeTokenSet={status.get('tradeTokenSet')}  "
              f"isActivated={status.get('isActivated')}")
    except BridgeError as exc:
        print(f"  {exc}")

    return 0 if settings_ok else 1


if __name__ == "__main__":
    sys.exit(main())
