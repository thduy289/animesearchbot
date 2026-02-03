import os
import discord
import aiohttp 
import json
import unicodedata
import re
from discord import app_commands
from discord.ext import tasks
from discord.ui import View, Select
from dotenv import load_dotenv

# Bot sẽ tự tìm Token trong hệ thống Environment Variable của Discloud
load_dotenv() 
TOKEN = os.getenv('DISCORD_TOKEN')
NOTION_TOKEN = os.getenv('NOTION_TOKEN')
DATABASE_ID = os.getenv('NOTION_DATABASE_ID')
WEB_BASE_URL = "https://rmbd.onrender.com"
CHANNEL_ID = os.getenv('CHANNEL_ID')
CACHE_FILE = "cache.json"

intents = discord.Intents.default()
intents.message_content = True 

# --- QUẢN LÝ CACHE ---
def load_cache():
    if not os.path.exists(CACHE_FILE): return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except: return {}

def save_cache(cache_data):
    with open(CACHE_FILE, "w", encoding="utf-8") as f: json.dump(cache_data, f, ensure_ascii=False, indent=4)

# --- CLIENT ---
class MyClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def on_ready(self):
        print(f'Bot đã online: {self.user}')
        await self.tree.sync()
        
        # Nếu chưa có cache -> Chạy đồng bộ lần đầu để không spam
        if not os.path.exists(CACHE_FILE):
            print("⚠️ Chạy lần đầu: Đang đồng bộ dữ liệu...")
            await sync_initial_data()
        else:
            print("✅ Đã có dữ liệu cũ.")

        if not check_new_anime.is_running():
            check_new_anime.start()
            print('⏰ Đã bật chế độ tự động kiểm tra.')

client = MyClient()

# --- LOGIC NOTION (Pagination & Fetch) ---
async def fetch_notion(payload):
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    headers = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200: return None
            return await resp.json()

async def fetch_all_pages(filter_payload=None):
    """Hàm lấy toàn bộ dữ liệu (không bị giới hạn 100 dòng)"""
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    headers = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
    results = []
    has_more = True
    cursor = None
    payload = { "page_size": 100 }
    if filter_payload: payload.update(filter_payload)
    async with aiohttp.ClientSession() as session:
        while has_more:
            if cursor: payload["start_cursor"] = cursor
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200: break
                data = await resp.json()
                if "results" in data: results.extend(data["results"])
                has_more = data.get("has_more", False)
                cursor = data.get("next_cursor")
    return results

def get_prop(page, prop_name):
    props = page.get("properties", {})
    prop = props.get(prop_name)
    if not prop: return "N/A"
    ptype = prop.get("type")
    if ptype == "title": return prop["title"][0]["plain_text"] if prop["title"] else "Không tên"
    elif ptype == "rich_text": return prop["rich_text"][0]["plain_text"] if prop["rich_text"] else "Không có"
    elif ptype == "number": return str(prop["number"]) if prop["number"] is not None else "?"
    elif ptype == "select": return prop["select"]["name"] if prop["select"] else "Không rõ"
    elif ptype == "url": return prop["url"] if prop["url"] else None
    elif ptype == "checkbox": return prop["checkbox"]
    elif ptype == "files":
        if prop["files"]:
            f = prop["files"][0]
            if "file" in f: return f["file"]["url"]
            if "external" in f: return f["external"]["url"]
    elif ptype == "date": return prop["date"]["start"] if prop["date"] else None
    return "N/A"

def create_slug_url(title, page_id):
    value = str(title)
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
    value = re.sub(r'[^\w\s-]', '', value.lower())
    slug = re.sub(r'[-\s]+', '-', value).strip('-')
    return f"{slug}-{page_id[-4:]}"

async def get_series_list(series_name, current):
    if series_name in ["Không có", "N/A", None]: return []
    payload = {"filter": {"and": [{"property": "Loạt phim", "rich_text": {"equals": series_name}}, {"property": "Public", "checkbox": {"equals": True}}]}}
    data = await fetch_notion(payload)
    if not data or not data.get("results"): return []
    return [get_prop(p, "Tên Romanji") for p in data["results"] if get_prop(p, "Tên Romanji") != current]

async def create_anime_embed(page, web_link):
    ten = get_prop(page, "Tên Romanji")
    embed = discord.Embed(title=f"🎬 {ten}", color=0x00b0f4, url=web_link)
    embed.add_field(name="Tiến độ", value=f"{get_prop(page, 'Số tập Vietsub')}/{get_prop(page, 'Số tập')}", inline=True)
    embed.add_field(name="Năm", value=get_prop(page, "Năm"), inline=True)
    img = get_prop(page, "Ảnh")
    if img != "N/A": embed.set_thumbnail(url=img)
    return embed

async def sync_initial_data():
    """Chạy lần đầu để nhớ hết các phim đang có"""
    payload = {"filter": {"property": "Public", "checkbox": {"equals": True}}}
    all_pages = await fetch_all_pages(payload)
    cache = {p["id"]: get_prop(p, "Ngày cập nhật") for p in all_pages if get_prop(p, "Ngày cập nhật")}
    save_cache(cache)
    print(f"--> Đã lưu trữ {len(cache)} phim vào bộ nhớ.")

@tasks.loop(minutes=10)
async def check_new_anime():
    if not CHANNEL_ID: return
    # Lấy toàn bộ phim Public
    all_pages = await fetch_all_pages({"filter": {"property": "Public", "checkbox": {"equals": True}}})
    if not all_pages: return
    
    local_cache = load_cache()
    has_changes = False
    channel = client.get_channel(int(CHANNEL_ID))
    if not channel: return

    for page in all_pages:
        pid = page["id"]
        new_date = get_prop(page, "Ngày cập nhật")
        
        # Bỏ qua nếu không có ngày
        if not new_date: continue
        
        old_date = local_cache.get(pid)

        # Logic: Chưa có trong cache HOẶC Ngày mới khác ngày cũ
        if (pid not in local_cache) or (new_date != old_date):
            print(f"🔔 Update: {get_prop(page, 'Tên Romanji')}")
            web_link = f"{WEB_BASE_URL}/anime/{create_slug_url(get_prop(page, 'Tên Romanji'), pid)}"
            embed = await create_anime_embed(page, web_link)
            
            if pid not in local_cache: 
                embed.set_author(name="🔥 Anime Mới!", icon_url="https://cdn-icons-png.flaticon.com/512/2965/2965358.png")
            else: 
                embed.set_author(name="🔄 Cập Nhật!", icon_url="https://cdn-icons-png.flaticon.com/512/1680/1680899.png")
            
            series = await get_series_list(get_prop(page, "Loạt phim"), get_prop(page, "Tên Romanji"))
            view = AnimeView(series)
            
            await channel.send(embed=embed, view=view)
            
            local_cache[pid] = new_date
            has_changes = True

    if has_changes: save_cache(local_cache)

# --- COMMANDS ---
class SeriesSelect(Select):
    def __init__(self, movies):
        options = [discord.SelectOption(label=m[:100]) for m in movies[:25]]
        super().__init__(placeholder="Cùng loạt phim", options=options)
    async def callback(self, itr):
        await itr.response.defer()
        # (Giản lược logic view cho ngắn gọn, bạn dùng lại logic cũ ở đây nếu cần)

class AnimeView(View):
    def __init__(self, movies):
        super().__init__(timeout=600)
        if movies: self.add_item(SeriesSelect(movies))

@client.tree.command(name="timphim")
async def timphim(itr: discord.Interaction, ten: str):
    await itr.response.defer()
    await itr.followup.send(f"Đang tìm: {ten}") # Code placeholder

client.run(TOKEN)