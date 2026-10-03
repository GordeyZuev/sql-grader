# Выкладка SQL Trainer

Обновление ручное. Изменения попадают на ВМ, когда вы сами заходите по SSH и запускаете обновление. GitHub Actions и реестра образов нет.

База кабинета, копии и сертификаты Caddy лежат на отдельном диске, смонтированном в `/srv/sql-trainer`. Диск должен быть подключён до старта Docker. `docker compose down -v` не запускайте: эта команда удаляет данные томов.

## Первый раз на ВМ

На машине уже есть Docker Compose, пользователь `deploy` в группе Docker, файл `/opt/sql-trainer/.env` и диск с данными. Если репозитория ещё нет:

```sh
sudo apt-get update
sudo apt-get install -y git make
sudo -u deploy git clone https://github.com/GordeyZuev/sql-grader.git /opt/sql-trainer/app
sudo chown -R deploy:docker /opt/sql-trainer/app
```

Если каталог уже есть, зайдите в него и проверьте `git remote -v`. Для закрытого репозитория у `deploy` нужен отдельный ключ только на чтение. Личный ключ администратора ВМ на GitHub не копируйте.

`.env` лежит вне репозитория, в `/opt/sql-trainer/.env`. В нём обязательны `CABINET_LEARNING_DSN` и `CABINET_ADMIN_TOKEN`. Роль в DSN только на чтение. `CABINET_ADMIN_NAME` можно не задавать. База кабинета: `/srv/sql-trainer/data/cabinet.sqlite3`.

Перед стартом проверьте диск:

```sh
findmnt /srv/sql-trainer
cd /opt/sql-trainer/app
make prod-up
make prod-status
```

Запись `A` для `hse-ai-sql.ru` должна указывать на зарезервированный адрес ВМ. Caddy получит сертификат, когда DNS уже смотрит на машину и снаружи открыты порты 80 и 443.

Боевой манифест в образ не копируется. Кабинет берёт опубликованную версию из SQLite. Если версии ещё не было, до пересборки положите JSON на диск данных:

```sh
install -o 10001 -g 10001 -m 0600 /path/to/manifest.course.json /srv/sql-trainer/data/manifest.json
```

## Обновление

Запушьте код в `main`, затем на ВМ:

```sh
cd /opt/sql-trainer/app
make update
```

Команда сначала делает проверенную копию, затем подтягивает `main`, собирает образ на самой ВМ, применяет схему и пересоздаёт сервисы. Базу и файлы на диске она не удаляет. Если `git pull` сообщает о конфликте или местных правках, остановитесь и посмотрите их. Сбрасывать каталог вслепую не нужно.

Полезные команды из `/opt/sql-trainer/app`:

```sh
make migrate       # остановить приложение и применить схему; снова поднять: make prod-up
make prod-status   # состояние контейнеров
make prod-logs     # журналы сервисов
make prod-backup   # копия SQLite прямо сейчас
make prod-down     # остановить сервисы, данные на диске остаются
make prod-up       # схема, сборка и старт
```

Схема применяется и при старте приложения, повторный запуск её не портит. `make migrate` останавливает кабинет и резервное копирование. После него снова нужен `make prod-up` или `make update`. Откат кода старую схему базы назад не разворачивает, поэтому перед обновлением нужна свежая копия.

Новые пределы процессов контейнера из `compose.prod.yml` начинают действовать после пересоздания контейнера, то есть после `make update` или `make prod-up`.

## Копии и восстановление

База и семь дневных копий лежат под `/srv/sql-trainer`. Перезагрузка ВМ их не стирает: диск монтируется по UUID. Потеря или удаление этого диска забирает и базу, и копии. Снять копию вне ВМ по-прежнему нужно вручную. Имеет смысл иногда копировать каталог или включить снимки диска в Yandex Cloud.

Проверка:

```sh
findmnt -no SOURCE,FSTYPE,UUID,TARGET /srv/sql-trainer
sudo ls -lh /srv/sql-trainer/backups
make prod-backup
```

Восстановление выбранного снимка:

```sh
docker compose --env-file /opt/sql-trainer/.env -f compose.prod.yml stop cabinet
docker compose --env-file /opt/sql-trainer/.env -f compose.prod.yml run --rm --no-deps backup \
  python -m cabinet.restore /backups/cabinet-YYYYMMDDTHHMMSSZ.sqlite3 --confirm
docker compose --env-file /opt/sql-trainer/.env -f compose.prod.yml start cabinet
```

После восстановления проверьте вход, опубликованный курс, попытки и журнал.

## Что держать закрытым

`.env` не коммитьте и оставьте читаемым только оператору ВМ. Если секрет уже светился в переписке, замените его.

У учебной PostgreSQL отдельная роль только на чтение и TLS (`sslmode=require`).

SSH снаружи ограничьте своим административным адресом. Наружу нужны порты 80 и 443 у Caddy. Порт 8000 не открывайте.

Группа Docker даёт права, близкие к root. В ней только те, кто и так администрирует машину.

Ограничение входа живёт в памяти одного процесса кабинета. Для этой одной ВМ его достаточно. Несколько процессов Uvicorn один файл SQLite между собой не делят.
