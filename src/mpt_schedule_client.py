import argparse
from dataclasses import dataclass
from typing import Dict, Optional

import requests
from bs4 import BeautifulSoup

SCHEDULE_URL = "https://mpt.ru/raspisanie/"


@dataclass
class ScheduleTarget:
    title: str
    anchor_id: str

    @property
    def deep_link(self) -> str:
        return f"{SCHEDULE_URL}#{self.anchor_id}"


class MptScheduleClient:
    """Клиент для извлечения расписания с https://mpt.ru/raspisanie/."""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def fetch_page(self) -> BeautifulSoup:
        response = requests.get(SCHEDULE_URL, timeout=self.timeout)
        response.raise_for_status()
        return BeautifulSoup(response.text, "lxml")

    def collect_anchors(self, soup: BeautifulSoup) -> Dict[str, ScheduleTarget]:
        """
        Собирает все якорные ссылки вида #hash и возвращает индекс по названию.
        Ключи индекса приводятся к lowercase для удобного поиска.
        """
        targets: Dict[str, ScheduleTarget] = {}
        for link in soup.select('a[href^="#"]'):
            href = link.get("href", "")
            title = " ".join(link.get_text(" ", strip=True).split())
            anchor_id = href[1:]
            if not title or not anchor_id:
                continue
            targets[title.lower()] = ScheduleTarget(title=title, anchor_id=anchor_id)
        return targets

    def find_target(self, targets: Dict[str, ScheduleTarget], query: str) -> Optional[ScheduleTarget]:
        query = query.lower().strip()
        if query in targets:
            return targets[query]

        for key, target in targets.items():
            if query in key:
                return target
        return None

    def extract_section_text(self, soup: BeautifulSoup, anchor_id: str) -> Optional[str]:
        section = soup.find(id=anchor_id)
        if not section:
            return None
        return section.get_text("\n", strip=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Поиск отделения/группы на mpt.ru/raspisanie")
    parser.add_argument("--department", default="09.02.06 СА", help="Название отделения")
    parser.add_argument("--group", default="СА-1-23; СА-11/1-24", help="Название группы")
    args = parser.parse_args()

    client = MptScheduleClient()
    soup = client.fetch_page()
    targets = client.collect_anchors(soup)

    department = client.find_target(targets, args.department)
    group = client.find_target(targets, args.group)

    if department:
        print(f"[Отделение] {department.title}")
        print(f"Ссылка: {department.deep_link}\n")
    else:
        print(f"Отделение '{args.department}' не найдено.\n")

    if group:
        print(f"[Группа] {group.title}")
        print(f"Ссылка: {group.deep_link}\n")
        text = client.extract_section_text(soup, group.anchor_id)
        if text:
            print("Фрагмент расписания:")
            print(text[:2500])
    else:
        print(f"Группа '{args.group}' не найдена.")


if __name__ == "__main__":
    main()
