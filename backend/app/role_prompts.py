from hashlib import sha256
from sqlite3 import Connection


# Replace only the known previous prompts. A user's edits take precedence.
ROLE_UPDATES = {
    "Neo": (
        (
            "959a94e31545391e7513ae540e24b6f3051bd33cf753395823609bacdfb45747",
            "89d483c1d616f7306e6f86beb9c4497e77071711209ffc674b3afbb0904e8937",
            "b3418b0dff40b27f832d90997ab500986b46c39380b74269fd826e1efda36cd3",
            "3d15ddadeb0e63597467392920159546e930b5e26ea15e42c1bbbf03dddcb7bf",
            "8b90bc2a08f440676a6acc7d72eb4eff3e96091d68c2f236e8034543eef3b91b",
        ),
        """Ты автор технического документа. Пиши по-русски для разработчика и автора задачи.
Читай checkout для проверки фактов. Записывай результат только в артефакты документации.""",
    ),
    "Myabi": (
        (
            "bce23c021e0b34e70cb7dc47fd2d50e17c24f036e7d8f0ebe624d5200e82c445",
            "7afdcc2b55d69f7d0eb5f20c07e6c6aebf160c2984a817d2d6303d4f7218a409",
            "4a765ac5e470edf8a9484b28bb8e32bb4f6d8d7d6803d90bd9fe9d8530f51af4",
        ),
        """Ты ревьюер техдокумента и реализации. Сверяй результат с дизайн-документом, ТЗ и согласованными ответами.
Выполняй проверку текущего этапа и записывай результат в указанный файл вердикта.""",
    ),
    "Kimi": (
        (
            "ff10cf9e4558012d4047ee557ad5e7671fc419bebb780bd423fb5b9a2f59eacd",
            "7234ac3a2811f7d98eccfff5ca6fcca5b1f024f8b5733d920179678a3c4bf5d8",
        ),
        """Ты исполнитель задачи в выделенном worktree. Прочитай AGENTS.md и проверь git status.
Сохрани чужие изменения. При конфликте с ними сообщи о блокере и завершись с ошибкой.
Работай через обычные точки входа. Сохраняй существующие контракты и связи ассетов. Обнови документацию, если меняется описанное поведение.
Возьми исходные данные, действие и ожидаемый результат из сценариев техдокумента. Используй существующие проверки проекта. Тест добавляй для требования или конкретной регрессии без покрытия.
Проверяй клик через рабочий интерфейс. Прямой вызов обработчика покрывает только логику обработчика. Для Unity CLI укажи -projectPath этого worktree.
При блокере сообщи факт и нужное действие. При успехе оставь результат по контракту воркера.""",
    ),
}


def update_role_prompts(conn: Connection) -> None:
    profiles = conn.execute("SELECT id, name, role_prompt FROM agents WHERE name IN ('Neo', 'Myabi', 'Kimi')").fetchall()
    for profile in profiles:
        previous_hashes, prompt = ROLE_UPDATES[profile["name"]]
        if sha256(profile["role_prompt"].encode("utf-8")).hexdigest() in previous_hashes:
            conn.execute(
                "UPDATE agents SET role_prompt=? WHERE id=? AND role_prompt=?",
                (prompt, profile["id"], profile["role_prompt"]),
            )
