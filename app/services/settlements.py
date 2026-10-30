"""Observed monetary reconciliations; no inferred rates or current-tariff fallback."""

from app.services.statistics import amount_or_none, parsed_time


def sale_settlement(gross, transaction):
    gross = amount_or_none(gross)
    result = {"gross": gross, "credit": None, "difference": None, "fee": None, "reconciled": False}
    if (
        not transaction
        or transaction["operation"] != "SELL"
        or transaction["direction"] != "IN"
        or transaction["status"] != "CONFIRMED"
        or transaction["currency"] != "RUB"
        or parsed_time(transaction["completed_at"]) is None
    ):
        return {**result, "reason": "Нет подтверждённого рублёвого SELL с датой завершения"}
    credit, fee = amount_or_none(transaction["value"]), amount_or_none(transaction["reported_fee"])
    result["credit"] = credit
    if gross is None or credit is None or credit > gross:
        return {**result, "reason": "Цена/зачисление неизвестны или противоречат друг другу"}
    result["difference"] = gross - credit
    if fee is None or fee != result["difference"]:
        return {**result, "reason": "Поле fee отсутствует или не совпадает с ценой минус зачисление"}
    return {**result, "fee": fee, "reconciled": True, "reason": None}


def withdrawal_settlement(transaction):
    if (
        transaction["operation"] != "WITHDRAW"
        or transaction["direction"] != "OUT"
        or transaction["status"] != "CONFIRMED"
        or transaction["currency"] != "RUB"
    ):
        return None
    debit, net = amount_or_none(transaction["value"]), amount_or_none(transaction["withdrawal_net_rub"])
    # The public wallet formatter treats zero net conversion fields as absent.
    if debit is None or net is None or net > debit or (debit > 0 and net == 0):
        return None
    return {"debit": debit, "net": net, "retained": debit - net}
