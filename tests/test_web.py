"""Тесты CRUD реестра компаний через FastAPI TestClient (in-memory SQLite)."""

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from portier.models import Base, Company
from portier.web import create_app, seed_companies


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
def client(session_factory):
    with TestClient(create_app(session_factory)) as c:
        yield c


def _count(response_html: str) -> int:
    return response_html.count("/edit")


def test_list_empty(client):
    resp = client.get("/companies")
    assert resp.status_code == 200
    assert "Пока нет ни одной компании" in resp.text


def test_create_company(client, session_factory):
    resp = client.post("/companies/new", follow_redirects=False, data={
        "name": "ООО «Ромашка»", "inn": "7712345678",
        "details": "р/с 123", "email": "buh@romashka.ru",
        "subject_template": "Счёт за проживание",
    })
    assert resp.status_code == 303

    resp = client.get("/companies")
    assert "ООО «Ромашка»" in resp.text
    assert "7712345678" in resp.text
    assert "buh@romashka.ru" in resp.text


def test_edit_company(client):
    client.post("/companies/new", follow_redirects=False, data={"name": "Старое название", "inn": "1",
                                        "details": "", "email": "", "subject_template": ""})
    resp = client.get("/companies")
    assert "Старое название" in resp.text

    # id первой записи = 1
    form = client.get("/companies/1/edit")
    assert form.status_code == 200
    assert "Старое название" in form.text

    resp = client.post("/companies/1/edit", follow_redirects=False, data={
        "name": "Новое название", "inn": "2", "details": "банк",
        "email": "a@b.c", "subject_template": "Тема",
    })
    assert resp.status_code == 303

    resp = client.get("/companies")
    assert "Новое название" in resp.text
    assert "Старое название" not in resp.text


def test_delete_company(client):
    client.post("/companies/new", follow_redirects=False, data={"name": "На удаление", "inn": "",
                                        "details": "", "email": "", "subject_template": ""})
    assert "На удаление" in client.get("/companies").text

    resp = client.post("/companies/1/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert "На удаление" not in client.get("/companies").text


def test_edit_missing_company_redirects(client):
    resp = client.get("/companies/999/edit", follow_redirects=False)
    assert resp.status_code == 303


def test_root_redirects(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303 or resp.is_redirect


async def test_seed_from_yaml(session_factory, tmp_path):
    seed_file = tmp_path / "companies.yaml"
    seed_file.write_text(yaml.safe_dump([
        {"name": "ООО «Сид»", "inn": "123", "email": "s@b.c"},
        {"name": "ИП Сидоров"},
    ], allow_unicode=True), encoding="utf-8")

    added = await seed_companies(session_factory, str(seed_file))
    assert added == 2

    async with session_factory() as session:
        from sqlalchemy import select
        names = (await session.execute(select(Company.name))).scalars().all()
    assert set(names) == {"ООО «Сид»", "ИП Сидоров"}

    # Повторный сид не дублирует
    assert await seed_companies(session_factory, str(seed_file)) == 0


async def test_seed_missing_file(session_factory):
    assert await seed_companies(session_factory, "нет_такого_файла.yaml") == 0
