# Shop Order Bot

A small Telegram storefront. Customers browse a catalog, order and chat with the shop;
admins add products and handle orders without leaving Telegram. Sells anything:
digital goods, services, physical products.

## Features
**Customers**
- Catalog with pagination, product pages with photos, stock and price
- Quantity picker, optional order details (text or photo), confirmation step
- "My orders" with live status notifications (accepted, in progress, completed, ...)
- Cancel pending orders, chat with the shop (general support or per order)

**Admins**
- **Quick add:** send products as text, one per line, even dozens at once
  ```
  Premium account | 9.99 | Lifetime access | 10
  T-shirt | 15
  ```
  Format: `Name | Price | Description | Stock` (description and stock optional)
- Edit name / price / description / stock / photo, hide or delete products
- Order management: accept, reject, in progress, complete, cancel (stock is returned
  automatically on reject/cancel)
- New-order notifications with one-tap reply, two-way chat with customers
- Customer list with ban / unban
- Anti-spam rate limit, brute-force lock on admin login

**Under the hood**
- Pure `pyTelegramBotAPI`, one file (`bot.py`)
- Storage: local JSON file (atomic writes) or MongoDB Atlas for hosts with ephemeral disks
- All secrets in `.env`, nothing hard-coded

## Quick start
```bash
git clone https://github.com/Kverty125/shop-order-bot.git
cd shop-order-bot
pip install -r requirements.txt
cp .env.example .env      # Windows: copy .env.example .env
# fill in BOT_TOKEN (from @BotFather) and ADMIN_PASSWORD
python bot.py
```
Then open your bot in Telegram, send `/admin`, enter the password, press **➕ Add product**.

Requires Python 3.9+.

## Admin guide
| Button | What it does |
|---|---|
| ➕ Add product | send product lines (bulk), photo + caption for a single product with image |
| 📦 Products | open a product: hide/show, edit fields, delete |
| 📊 Orders | active / all orders, change status, message the customer |
| 👥 Users | customers list, ban / unban, send a message |

Commands: `/start`, `/orders`, `/support`, `/help`, `/admin`, `/cancel`.

## Configuration (`.env`)
| Variable | Description |
|---|---|
| `BOT_TOKEN` | bot token, **required** |
| `ADMIN_PASSWORD` | password for `/admin` (empty = disabled) |
| `ADMIN_IDS` | comma-separated admin Telegram IDs |
| `SHOP_NAME`, `WELCOME_TEXT`, `ABOUT_TEXT` | storefront texts |
| `PRICE_FORMAT` | e.g. `${amount}` or `{amount} EUR` |
| `MONGO_URL`, `MONGO_DB` | use MongoDB instead of a local file |
| `DATA_FILE` | JSON file path (default `shop_data.json`) |
| `ENABLE_WEB`, `PORT` | tiny web server for hosts that need an open port |

## Deploying (Render, etc.)
Free hosts wipe the local disk on restart, so set `MONGO_URL` (MongoDB Atlas free tier works)
and `ENABLE_WEB=1`. If `MONGO_URL` is set but the connection fails, the bot exits instead of
silently using a local file.

## Security
- Never commit `.env`. If a token or database password leaks, rotate it
  (@BotFather → `/revoke`, Atlas → Database Access).
- Use a long random `ADMIN_PASSWORD`. The bot deletes your password message after login.

## License
MIT
