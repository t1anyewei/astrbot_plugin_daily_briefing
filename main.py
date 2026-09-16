import asyncio
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
POLL_INTERVAL_SECONDS = 20


class DailyBriefingPlugin(Star):
    """A daily briefing for subscribed OneBot QQ groups."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._last_sent_date: date | None = None
        context.register_web_api(
            "/astrbot_plugin_daily_briefing/holidays",
            self.get_holidays,
            ["GET"],
            "Get daily briefing holiday periods",
        )
        context.register_web_api(
            "/astrbot_plugin_daily_briefing/holidays/save",
            self.save_holidays,
            ["POST"],
            "Save daily briefing holiday periods",
        )
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def get_holidays(self):
        """Return holiday periods for the plugin Page."""
        return json_response({"periods": self._holiday_periods()})

    async def save_holidays(self):
        """Validate and save holiday periods submitted from the plugin Page."""
        payload = await request.json(default={})
        periods = payload.get("periods") if isinstance(payload, dict) else None
        if not isinstance(periods, list):
            return error_response("periods must be a list")

        validated: list[dict[str, str]] = []
        for item in periods:
            if not isinstance(item, dict):
                return error_response("each holiday must be an object")
            name = str(item.get("name", "")).strip()
            start = str(item.get("start", "")).strip()
            end = str(item.get("end", "")).strip()
            if not name or len(name) > 40:
                return error_response("holiday name must be 1 to 40 characters")
            try:
                start_date = date.fromisoformat(start)
                end_date = date.fromisoformat(end)
            except ValueError:
                return error_response("dates must use YYYY-MM-DD")
            if end_date < start_date:
                return error_response("holiday end date cannot be earlier than start date")
            if (end_date - start_date).days > 31:
                return error_response("a holiday period cannot be longer than 32 days")
            validated.append({"name": name, "start": start, "end": end})

        validated.sort(key=lambda item: (item["start"], item["end"], item["name"]))
        self.config["holiday_periods"] = validated
        self.config.save_config()
        return json_response({"periods": validated, "saved": True})

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("订阅日报")
    async def subscribe_daily_briefing(self, event: AstrMessageEvent):
        """订阅当前 QQ 群的每日播报。"""
        subscriptions = self._subscriptions()
        umo = event.unified_msg_origin
        if any(item.get("umo") == umo for item in subscriptions):
            yield event.plain_result("本群已经订阅每日播报。")
            return

        subscriptions.append({
            "umo": umo,
            "group_id": str(event.message_obj.group_id),
        })
        self._save_subscriptions(subscriptions)
        yield event.plain_result(
            f"已订阅本群每日播报，当前发送时间为 {self._broadcast_time_text()}。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("取消订阅日报")
    async def unsubscribe_daily_briefing(self, event: AstrMessageEvent):
        """取消当前 QQ 群的每日播报。"""
        umo = event.unified_msg_origin
        subscriptions = self._subscriptions()
        remaining = [item for item in subscriptions if item.get("umo") != umo]
        if len(remaining) == len(subscriptions):
            yield event.plain_result("本群当前未订阅每日播报。")
            return

        self._save_subscriptions(remaining)
        yield event.plain_result("已取消本群的每日播报订阅。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("修改每日报时时间")
    async def set_broadcast_time(self, event: AstrMessageEvent, broadcast_time: str):
        """修改每日播报时间。用法：/修改每日报时时间 07:00。"""
        parsed = self._parse_time(broadcast_time)
        if parsed is None:
            yield event.plain_result("时间格式无效，请使用 24 小时制 HH:MM，例如 /修改每日报时时间 07:00。")
            return

        self.config["broadcast_time"] = parsed.strftime("%H:%M")
        self.config.save_config()
        self._last_sent_date = None
        yield event.plain_result(f"每日播报时间已修改为 {parsed:%H:%M}（北京时间）。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("每日播报状态")
    async def briefing_status(self, event: AstrMessageEvent):
        """查看本群的日报订阅状态。"""
        subscribed = any(item.get("umo") == event.unified_msg_origin for item in self._subscriptions())
        holiday_count = len(self._holiday_dates())
        status = "已订阅" if subscribed else "未订阅"
        yield event.plain_result(
            f"本群每日播报：{status}\n发送时间：{self._broadcast_time_text()}（北京时间）\n"
            f"日报人格：{self.config.get('persona_id') or 'default'}\n"
            f"已配置法定节假日日期：{holiday_count} 天"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("立即发送每日播报")
    async def send_now(self, event: AstrMessageEvent):
        """立即向当前群发送一次每日播报。"""
        await self._send_briefing(event.unified_msg_origin)
        yield event.plain_result("已发送今日播报。")

    async def _scheduler_loop(self):
        while True:
            try:
                now = datetime.now(SHANGHAI_TZ)
                target = self._parse_time(str(self.config.get("broadcast_time", "07:00")))
                if target and now.time().replace(second=0, microsecond=0) >= target:
                    if self._last_sent_date != now.date():
                        await self._send_to_all_subscribers()
                        self._last_sent_date = now.date()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("每日群播报调度失败")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _send_to_all_subscribers(self):
        for subscription in self._subscriptions():
            umo = subscription.get("umo")
            if not isinstance(umo, str) or not umo:
                continue
            try:
                await self._send_briefing(umo)
            except Exception:
                logger.exception("向订阅群发送每日播报失败：%s", subscription.get("group_id"))

    async def _send_briefing(self, umo: str):
        facts = self._briefing_facts(datetime.now(SHANGHAI_TZ).date())
        content = await self._generate_persona_message(facts)
        await self.context.send_message(umo, MessageChain().message(content))

    async def _generate_persona_message(self, facts: str) -> str:
        provider_id = str(self.config.get("chat_provider_id", "")).strip()
        if not provider_id:
            return facts

        persona_id = str(self.config.get("persona_id", "default")).strip()
        persona = self.context.persona_manager.get_persona_v3_by_id(persona_id)
        if persona:
            system_prompt = persona["prompt"]
        else:
            system_prompt = "你是友好、简洁的群聊日报助手。"

        prompt = (
            "请依据以下已核实的日报事实，写一条适合 QQ 群早晨发送的简短中文播报。"
            "不得改变、遗漏或编造日期和倒计时数据；不使用 Markdown 标题；只输出最终播报文本。\n\n"
            f"{facts}"
        )
        try:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
            )
            text = (response.completion_text or "").strip()
            return text or facts
        except Exception:
            logger.exception("日报 AI 生成失败，改用事实文本")
            return facts

    def _briefing_facts(self, today: date) -> str:
        weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        days_to_weekend = 0 if today.weekday() >= 5 else 5 - today.weekday()
        weekend_text = "今天就是周末" if days_to_weekend == 0 else f"距离周末还有 {days_to_weekend} 天"
        holiday_text = self._next_holiday_text(today)
        return (
            f"今日日期：{today:%Y年%m月%d日}，{weekday_names[today.weekday()]}。\n"
            f"{weekend_text}。\n{holiday_text}"
        )

    def _next_holiday_text(self, today: date) -> str:
        future_dates = sorted(day for day in self._holiday_dates() if day >= today)
        if not future_dates:
            return "未配置未来法定节假日日期，请管理员在插件配置中更新年度放假安排。"
        next_day = future_dates[0]
        days = (next_day - today).days
        return "今天是法定节假日。" if days == 0 else f"距离下一法定节假日还有 {days} 天（{next_day:%Y年%m月%d日}）。"

    def _holiday_dates(self) -> set[date]:
        result: set[date] = set()
        for period in self._holiday_periods():
            start = date.fromisoformat(period["start"])
            end = date.fromisoformat(period["end"])
            for offset in range((end - start).days + 1):
                result.add(start + timedelta(days=offset))
        for value in self.config.get("holiday_dates", []):
            try:
                result.add(date.fromisoformat(str(value)))
            except ValueError:
                logger.warning("忽略无效的节假日日期配置：%s", value)
        return result

    def _holiday_periods(self) -> list[dict[str, str]]:
        raw = self.config.get("holiday_periods", [])
        if not isinstance(raw, list):
            return []
        periods: list[dict[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            start = str(item.get("start", "")).strip()
            end = str(item.get("end", "")).strip()
            try:
                if name and date.fromisoformat(end) >= date.fromisoformat(start):
                    periods.append({"name": name, "start": start, "end": end})
            except ValueError:
                logger.warning("忽略无效的节假日区间配置：%s", item)
        return periods

    def _subscriptions(self) -> list[dict]:
        raw = self.config.get("subscriptions", [])
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def _save_subscriptions(self, subscriptions: list[dict]):
        self.config["subscriptions"] = subscriptions
        self.config.save_config()

    def _broadcast_time_text(self) -> str:
        parsed = self._parse_time(str(self.config.get("broadcast_time", "07:00")))
        return parsed.strftime("%H:%M") if parsed else "07:00"

    @staticmethod
    def _parse_time(value: str) -> time | None:
        try:
            return datetime.strptime(value.strip(), "%H:%M").time()
        except ValueError:
            return None

    async def terminate(self):
        self._scheduler_task.cancel()
        try:
            await self._scheduler_task
        except asyncio.CancelledError:
            pass
