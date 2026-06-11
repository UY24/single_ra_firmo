# RabbitMQ via Docker

This project can run RabbitMQ from Docker while the Python app and worker run on your machine.

Start RabbitMQ:

```sh
cd website_url_finder
docker compose up -d rabbitmq
```

Check it is healthy:

```sh
docker compose ps
```

The app connects to `127.0.0.1:5672`, matching the existing `.env` values:

```env
RABBITMQ_HOST=127.0.0.1
RABBITMQ_PORT=5672
RABBITMQ_USER=<your-rabbitmq-user>
RABBITMQ_PASS=<your-rabbitmq-password>
RABBITMQ_VHOST=/
```

The RabbitMQ management UI is available at `http://localhost:15672`. Log in with the same `RABBITMQ_USER` and `RABBITMQ_PASS` from `.env`.

Run the app and worker in separate terminals:

```sh
cd website_url_finder
source .venv/bin/activate
python app.py
```

```sh
cd website_url_finder
source .venv/bin/activate
python worker.py
```

Stop RabbitMQ:

```sh
cd website_url_finder
docker compose down
```

If you change RabbitMQ credentials after the volume has already been created, reset the RabbitMQ data volume:

```sh
cd website_url_finder
docker compose down -v
docker compose up -d rabbitmq
```
