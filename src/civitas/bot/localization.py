"""Translate Discord text using the invoking user's client locale."""

import discord
from discord import app_commands

_MESSAGES = {
    "command.start.description": {
        "en": "Get started in Civitas",
        "ru": "Как начать игру в Civitas",
    },
    "command.citizen.description": {
        "en": "Civitas citizenship",
        "ru": "Гражданство Civitas",
    },
    "command.register.description": {
        "en": "Register for citizenship",
        "ru": "Оформить гражданство",
    },
    "command.profile.description": {
        "en": "Check your citizenship status",
        "ru": "Проверить свой гражданский статус",
    },
    "command.wallet.description": {"en": "Your Civitas wallet", "ru": "Ваш кошелёк Civitas"},
    "command.wallet.balance.description": {"en": "View your balance", "ru": "Посмотреть свой баланс"},
    "command.wallet.history.description": {
        "en": "View your recent transactions",
        "ru": "Посмотреть свои последние операции",
    },
    "command.wallet.transfer.description": {
        "en": "Transfer money to a citizen",
        "ru": "Перевести деньги гражданину",
    },
    "command.wallet.transfer.recipient.description": {"en": "Citizen receiving the transfer", "ru": "Получатель перевода"},
    "command.wallet.transfer.amount.description": {"en": "Amount in whole units", "ru": "Сумма в целых единицах"},
    "command.jobs.description": {"en": "NPC jobs", "ru": "Работа у NPC"},
    "command.jobs.list.description": {"en": "Browse available jobs or view a job by ID", "ru": "Посмотреть доступные задания или открыть задание по номеру"},
    "command.jobs.claim.description": {"en": "Claim a job", "ru": "Взять задание"},
    "command.jobs.submit.description": {"en": "Submit completed work for review", "ru": "Сдать выполненное задание на проверку"},
    "command.jobs.staff.description": {"en": "Publish and review jobs", "ru": "Публикация и проверка заданий"},
    "command.jobs.staff.publish.description": {"en": "Publish an NPC job", "ru": "Опубликовать задание NPC"},
    "command.jobs.staff.review.description": {"en": "View the review queue or inspect submitted work privately", "ru": "Посмотреть очередь или приватно проверить сданную работу"},
    "command.jobs.staff.approve.description": {"en": "Approve submitted work and pay its reward", "ru": "Одобрить работу и выплатить награду"},
    "command.jobs.staff.reject.description": {"en": "Reject submitted work", "ru": "Отклонить сданную работу"},
    "command.jobs.task_id.description": {"en": "Job ID from `/jobs list`", "ru": "Номер задания из `/jobs list`"},
    "command.jobs.review_task_id.description": {"en": "Job ID from the review queue", "ru": "Номер задания из очереди проверки"},
    "command.jobs.proof.description": {"en": "Proof or a short description of the result", "ru": "Подтверждение или краткое описание результата"},
    "command.jobs.category.description": {"en": "Job category", "ru": "Категория задания"},
    "command.jobs.title.description": {"en": "Short title", "ru": "Краткое название"},
    "command.jobs.instructions.description": {"en": "What to do and how to prove completion", "ru": "Что сделать и как подтвердить результат"},
    "command.jobs.reward.description": {"en": "Reward in whole units", "ru": "Награда в целых единицах"},
    "command.jobs.hours.description": {"en": "Hours before the job expires", "ru": "Через сколько часов истекает срок задания"},
    "command.jobs.reason.description": {"en": "Reason for the review decision", "ru": "Причина решения по заявке"},
    "access.wrong_guild": {
        "en": "Civitas commands are available on the game server.",
        "ru": "Команды Civitas доступны на игровом сервере.",
    },
    "access.not_allowed": {
        "en": "Your account is not yet approved to join Civitas.",
        "ru": "Ваш аккаунт пока не допущен к участию в Civitas.",
    },
    "status.pending": {"en": "pending review", "ru": "ожидает проверки"},
    "status.verified": {"en": "verified", "ru": "подтверждено"},
    "status.suspended": {"en": "suspended", "ru": "приостановлено"},
    "status.left": {
        "en": "inactive after leaving the server",
        "ru": "неактивно после выхода с сервера",
    },
    "registration.created": {"en": "Citizenship granted.", "ru": "Гражданство оформлено."},
    "registration.restored": {"en": "Citizenship restored.", "ru": "Гражданство восстановлено."},
    "registration.existing": {
        "en": "You already have a citizenship record.",
        "ru": "Запись о вашем гражданстве уже есть.",
    },
    "registration.result": {
        "en": "{outcome} Citizenship status: {status}. Use `/profile` to check your current status.",
        "ru": "{outcome} Статус гражданства: {status}. Команда `/profile` покажет текущий статус.",
    },
    "start.welcome": {
        "en": "Welcome to Civitas! Use `/citizen register` to register for citizenship "
        "and `/profile` to check your status.",
        "ru": "Добро пожаловать в Civitas! Оформить гражданство можно командой "
        "`/citizen register`, проверить свой статус — командой `/profile`.",
    },
    "profile.missing": {
        "en": "Use `/citizen register` to register for citizenship.",
        "ru": "Чтобы оформить гражданство, воспользуйтесь командой `/citizen register`.",
    },
    "profile.status": {"en": "Citizenship status: {status}.", "ru": "Статус гражданства: {status}."},
    "error.temporary": {
        "en": "This command is temporarily unavailable. Please try again later.",
        "ru": "Команда временно недоступна. Попробуйте ещё раз позже.",
    },
    "wallet.balance": {"en": "Your balance: {amount}.", "ru": "Ваш баланс: {amount}."},
    "wallet.history.empty": {"en": "Your wallet has no transactions yet.", "ru": "В вашем кошельке пока нет операций."},
    "wallet.history.title": {"en": "Your recent transactions", "ru": "Ваши последние операции"},
    "wallet.history.genesis": {"en": "Initial issue", "ru": "Начальный выпуск"},
    "wallet.history.starter_grant": {"en": "Starter grant", "ru": "Стартовое начисление"},
    "wallet.history.npc_payout": {"en": "NPC work reward", "ru": "Награда за работу у NPC"},
    "wallet.history.transfer_in": {"en": "Transfer received", "ru": "Входящий перевод"},
    "wallet.history.transfer_out": {"en": "Transfer sent", "ru": "Исходящий перевод"},
    "wallet.transfer.preview": {
        "en": "Send {amount} to {recipient}?",
        "ru": "Перевести {amount} получателю {recipient}?",
    },
    "wallet.transfer.confirm": {"en": "Confirm", "ru": "Подтвердить"},
    "wallet.transfer.cancel": {"en": "Cancel", "ru": "Отменить"},
    "wallet.transfer.cancelled": {"en": "Transfer cancelled.", "ru": "Перевод отменён."},
    "wallet.transfer.expired": {
        "en": "Confirmation expired. Check `/wallet history` before starting another transfer.",
        "ru": "Срок подтверждения истёк. Проверьте `/wallet history` перед новым переводом.",
    },
    "wallet.transfer.wrong_actor": {
        "en": "Only the sender can confirm or cancel this transfer.",
        "ru": "Подтвердить или отменить этот перевод может только отправитель.",
    },
    "wallet.transfer.processing": {"en": "This transfer is being processed.", "ru": "Этот перевод уже обрабатывается."},
    "wallet.transfer.retry": {
        "en": "The transfer status is uncertain. Check `/wallet history` or use this confirmation again.",
        "ru": "Статус перевода пока неизвестен. Проверьте `/wallet history` или нажмите «Подтвердить» ещё раз.",
    },
    "wallet.transfer.done": {"en": "Sent {amount} to {recipient}.", "ru": "Перевод {amount} получателю {recipient} выполнен."},
    "wallet.error.citizen_not_verified": {
        "en": "You need verified citizenship to use the wallet.",
        "ru": "Кошелёк доступен после подтверждения гражданства.",
    },
    "wallet.error.recipient_not_verified": {
        "en": "The recipient needs verified citizenship.",
        "ru": "У получателя должно быть подтверждённое гражданство.",
    },
    "wallet.error.invalid_amount": {"en": "Enter a positive whole amount.", "ru": "Введите положительную целую сумму."},
    "wallet.error.self_transfer": {"en": "Choose another citizen.", "ru": "Выберите другого гражданина."},
    "wallet.error.invalid_transfer_key": {
        "en": "This confirmation is invalid. Open a new transfer.",
        "ru": "Подтверждение недействительно. Откройте новый перевод.",
    },
    "wallet.error.insufficient_funds": {"en": "Insufficient funds.", "ru": "Недостаточно средств."},
    "wallet.error.idempotency_conflict": {
        "en": "This confirmation conflicts with an existing transfer. Check `/wallet history`.",
        "ru": "Подтверждение конфликтует с уже записанным переводом. Проверьте `/wallet history`.",
    },
    "wallet.error.balance_overflow": {"en": "This transfer exceeds the wallet limit.", "ru": "Этот перевод превышает лимит кошелька."},
    "wallet.error.invalid_posting": {"en": "The transfer could not be recorded.", "ru": "Не удалось записать перевод."},
    "jobs.category.industry": {"en": "Industry", "ru": "Промышленность"},
    "jobs.category.logistics": {"en": "Logistics", "ru": "Логистика"},
    "jobs.category.services": {"en": "Services", "ru": "Услуги"},
    "jobs.category.events": {"en": "Events", "ru": "События"},
    "jobs.status.claimed": {"en": "claimed", "ru": "взято в работу"},
    "jobs.status.submitted": {"en": "awaiting review", "ru": "ожидает проверки"},
    "jobs.status.paid": {"en": "paid", "ru": "оплачено"},
    "jobs.status.rejected": {"en": "rejected", "ru": "отклонено"},
    "jobs.list.empty": {"en": "No jobs are available right now.", "ru": "Сейчас нет доступных заданий."},
    "jobs.list.title": {"en": "Available NPC jobs", "ru": "Доступные задания NPC"},
    "jobs.list.details": {"en": "Use `/jobs list task_id:` to view instructions for a job.", "ru": "Посмотрите условия задания через `/jobs list task_id:`."},
    "jobs.task.summary": {"en": "#{id} · {title} · {category} · {reward} · Due {deadline}", "ru": "№ {id} · {title} · {category} · {reward} · Срок {deadline}"},
    "jobs.task.status": {"en": "Job #{id}: {status}.", "ru": "Задание № {id}: {status}."},
    "jobs.task.reason": {"en": "Review reason: {reason}", "ru": "Причина решения: {reason}"},
    "jobs.claim.instructions": {"en": "Use `/jobs list task_id:{id}` for instructions and `/jobs submit` to send your result.", "ru": "Условия доступны через `/jobs list task_id:{id}`. Для сдачи работы используйте `/jobs submit`."},
    "jobs.task.published": {"en": "Job #{id} published.", "ru": "Задание № {id} опубликовано."},
    "jobs.queue.empty": {"en": "There is no submitted work to review.", "ru": "В очереди проверки пока нет сданных работ."},
    "jobs.queue.tasks": {"en": "Jobs awaiting review: {ids}. Use `/jobs staff review` with the job ID to inspect the work.", "ru": "Задания на проверке: {ids}. Посмотрите работу через `/jobs staff review` с номером задания."},
    "jobs.review.claimant": {"en": "Submitted by {claimant}.", "ru": "Исполнитель: {claimant}."},
    "jobs.review.proof": {"en": "Submitted proof\n{proof}", "ru": "Подтверждение результата\n{proof}"},
    "jobs.review.no_proof": {"en": "No proof has been submitted yet.", "ru": "Подтверждение результата ещё не сдано."},
    "jobs.error.citizen_not_verified": {"en": "You need verified citizenship to take jobs.", "ru": "Задания доступны после подтверждения гражданства."},
    "jobs.error.staff_required": {"en": "This action requires NPC job staff access.", "ru": "Для этого действия нужны полномочия сотрудника NPC."},
    "jobs.error.task_not_found": {"en": "Job not found.", "ru": "Задание не найдено."},
    "jobs.error.task_expired": {"en": "This job has expired.", "ru": "Срок задания истёк."},
    "jobs.error.task_already_claimed": {"en": "This job has already been claimed.", "ru": "Это задание уже взято в работу."},
    "jobs.error.claim_not_found": {"en": "No claim was found for this job.", "ru": "Для этого задания нет заявки."},
    "jobs.error.claim_not_owned": {"en": "Only the citizen who claimed the job can submit it.", "ru": "Сдать работу может только гражданин, взявший задание."},
    "jobs.error.invalid_status": {"en": "This action is unavailable at the current job status.", "ru": "Действие недоступно при текущем статусе задания."},
    "jobs.error.self_approval": {"en": "A different staff member must review your work.", "ru": "Вашу работу должен проверить другой сотрудник."},
    "jobs.error.claimant_not_verified": {"en": "The claimant's citizenship is not verified.", "ru": "Гражданство исполнителя не подтверждено."},
    "jobs.error.cooldown_active": {"en": "Please wait before claiming another job.", "ru": "Подождите, прежде чем брать следующее задание."},
    "jobs.error.invalid_reward": {"en": "The reward is outside the configured range.", "ru": "Награда вне настроенного диапазона."},
    "jobs.error.invalid_text": {"en": "Check that all required text is present and within the length limits.", "ru": "Заполните текстовые поля с учётом ограничений по длине."},
    "jobs.error.invalid_deadline": {"en": "Enter a valid future deadline.", "ru": "Укажите допустимый срок в будущем."},
    "jobs.error.invalid_category": {"en": "Choose a valid job category.", "ru": "Выберите допустимую категорию задания."},
    "jobs.error.overall_limit": {"en": "The weekly spending limit has been reached.", "ru": "Недельный лимит расходов исчерпан."},
    "jobs.error.npc_limit": {"en": "The weekly NPC job budget has been reached.", "ru": "Недельный бюджет NPC-работ исчерпан."},
    "jobs.error.personal_limit": {"en": "This payment would exceed the claimant's weekly reward limit.", "ru": "Выплата превысит недельный лимит наград исполнителя."},
    "jobs.error.budget_period_unavailable": {"en": "The next budget period has not started yet.", "ru": "Новый бюджетный период ещё не начался."},
    "jobs.error.treasury_insufficient_funds": {"en": "The treasury has insufficient funds for the reward.", "ru": "В казне недостаточно средств на выплату награды."},
}


def message(locale: discord.Locale | str | None, key: str, **fields: str) -> str:
    language = "ru" if locale in (discord.Locale.russian, "ru") else "en"
    return _MESSAGES[key][language].format(**fields)


def command_description(command: str) -> app_commands.locale_str:
    key = f"command.{command}.description"
    return app_commands.locale_str(message(None, key), key=key)


class CommandTranslator(app_commands.Translator):
    async def translate(
        self,
        string: app_commands.locale_str,
        locale: discord.Locale,
        context: app_commands.TranslationContext,
    ) -> str | None:
        key = string.extras.get("key")
        if locale == discord.Locale.russian and key is not None:
            return message(locale, key)
        return None
