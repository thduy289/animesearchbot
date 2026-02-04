import os
import discord
import aiohttp 
import random
import re
import json
import unicodedata
from discord import app_commands
from discord.ext import tasks
from discord.ui import View, Button, Select
from dotenv import load_dotenv
from keep_alive import keep_alive

# --- CẤU HÌNH ---
load_dotenv('token.env')
TOKEN = os.getenv('DISCORD_TOKEN')
NOTION_TOKEN = os.getenv('NOTION_TOKEN')
DATABASE_ID = os.getenv('NOTION_DATABASE_ID')
WEB_BASE_URL = "https://rmbd.onrender.com"
CHANNEL_ID = os.getenv('CHANNEL_ID')
CACHE_FILE = "cache.json" # File lưu trạng thái để so sánh ngày

intents = discord.Intents.default()
intents.message_content = True 

# ==========================================
# PHẦN 1: QUẢN LÝ CACHE (TRÍ NHỚ)
# ==========================================
def load_cache():
    """Đọc dữ liệu từ file JSON để biết ngày cũ là ngày nào"""
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_cache(cache_data):
    """Lưu dữ liệu mới vào file JSON"""
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache_data, f, ensure_ascii=False, indent=4)

# ==========================================
# PHẦN 2: CLIENT & SETUP
# ==========================================
class MyClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def on_ready(self):
        print(f'Bot đã online: {self.user}')
        await self.tree.sync()
        
        # Kiểm tra nếu chưa có cache thì chạy đồng bộ lần đầu
        if not os.path.exists(CACHE_FILE):
            print("⚠️ Chạy lần đầu: Đang đồng bộ dữ liệu để tránh spam...")
            await sync_initial_data()
        else:
            print("✅ Đã có dữ liệu cũ. Sẵn sàng hoạt động.")

        # Bật chế độ tự động kiểm tra
        if not check_new_anime.is_running():
            check_new_anime.start()
            print('⏰ Đã bật chế độ tự động kiểm tra (10 phút/lần).')

client = MyClient()

# ==========================================
# PHẦN 3: CÁC HÀM XỬ LÝ LOGIC NOTION
# ==========================================

async def fetch_notion(payload):
    """Gọi API Notion 1 lần (dùng cho tìm kiếm lẻ)"""
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                print(f"Lỗi API Notion: {resp.status}")
                return None
            return await resp.json()

async def fetch_all_pages(filter_payload=None):
    """
    [QUAN TRỌNG] Hàm lấy TOÀN BỘ dữ liệu bằng vòng lặp (Pagination)
    Khắc phục lỗi chỉ lấy được 100 dòng.
    """
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    
    results = []
    has_more = True
    cursor = None
    
    # Payload cơ bản
    payload = { "page_size": 100 }
    if filter_payload:
        payload.update(filter_payload)

    async with aiohttp.ClientSession() as session:
        while has_more:
            if cursor:
                payload["start_cursor"] = cursor
            
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    print(f"Lỗi khi tải trang: {resp.status}")
                    break
                data = await resp.json()
                
                if "results" in data:
                    results.extend(data["results"])
                
                has_more = data.get("has_more", False)
                cursor = data.get("next_cursor")
                
    return results

def get_prop(page, prop_name):
    """Lấy giá trị từ property Notion an toàn"""
    props = page.get("properties", {})
    prop = props.get(prop_name)
    if not prop: return "N/A"
    
    ptype = prop.get("type")
    
    if ptype == "title":
        return prop["title"][0]["plain_text"] if prop["title"] else "Không tên"
    elif ptype == "rich_text":
        return prop["rich_text"][0]["plain_text"] if prop["rich_text"] else "Không có"
    elif ptype == "number":
        return str(prop["number"]) if prop["number"] is not None else "?"
    elif ptype == "select":
        return prop["select"]["name"] if prop["select"] else "Không rõ"
    elif ptype == "multi_select":
        return ", ".join([o['name'] for o in prop['multi_select']]) if prop['multi_select'] else "Không rõ"
    elif ptype == "status":
        return prop["status"]["name"] if prop["status"] else "Không rõ"
    elif ptype == "url":
        return prop["url"] if prop["url"] else None
    elif ptype == "checkbox":
        return prop["checkbox"]
    elif ptype == "files":
        if prop["files"]:
            file_obj = prop["files"][0]
            if "file" in file_obj: return file_obj["file"]["url"]
            if "external" in file_obj: return file_obj["external"]["url"]
    elif ptype == "date":
        # Trả về ngày dạng chuỗi YYYY-MM-DD
        return prop["date"]["start"] if prop["date"] else None
        
    return "N/A"

def create_slug_url(title, page_id):
    value = str(title)
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
    value = re.sub(r'[^\w\s-]', '', value.lower())
    slug = re.sub(r'[-\s]+', '-', value).strip('-')
    suffix = page_id[-4:] 
    return f"{slug}-{suffix}"

async def get_series_list(series_name, current_movie_name):
    if series_name in ["Không có", "N/A", None]:
        return []
    payload = {
        "filter": {
            "and": [
                { "property": "Loạt phim", "rich_text": { "equals": series_name } },
                { "property": "Public", "checkbox": { "equals": True } }
            ]
        },
        "sorts": [{ "property": "Tên Romanji", "direction": "ascending" }]
    }
    data = await fetch_notion(payload)
    if not data or not data.get("results"):
        return []
    series_movies = []
    for p in data["results"]:
        name = get_prop(p, "Tên Romanji")
        if name != current_movie_name:
            series_movies.append(name)
    return series_movies

async def create_anime_embed(page, web_link):
    ten_romanji = get_prop(page, "Tên Romanji")
    ten_tieng_anh = get_prop(page, "Tên tiếng Anh")
    so_tap_sub = get_prop(page, "Số tập Vietsub")
    so_tap = get_prop(page, "Số tập")
    nam = get_prop(page, "Năm")
    link_tai = get_prop(page, "Tải xuống phụ đề") 
    anh_bia = get_prop(page, "Ảnh")
    tom_tat = get_prop(page, "Tóm tắt nội dung")
    trang_thai = get_prop(page, "Trạng thái")
    nhom_dich = get_prop(page, "Bản quyền/Nhóm dịch")

    embed = discord.Embed(title=f"🎬 {ten_romanji}", color=0x00b0f4, url=web_link)
    
    desc = ""
    if ten_tieng_anh != "Không có":
        desc += f"**Tên khác:** {ten_tieng_anh}\n"
    
    if tom_tat != "Không có":
        short = (tom_tat[:250] + '...') if len(tom_tat) > 250 else tom_tat
        desc += f"\n**Nội dung:**\n_{short}_\n"

    embed.description = desc
    
    embed.add_field(name="Tiến độ", value=f"{so_tap_sub}/{so_tap}", inline=True)
    embed.add_field(name="Năm", value=nam, inline=True)
    embed.add_field(name="Trạng thái", value=trang_thai, inline=True)
    
    if nhom_dich != "N/A" and nhom_dich != "Không rõ":
        embed.add_field(name="Nhóm dịch", value=nhom_dich, inline=True)

    if link_tai and link_tai != "N/A":
        embed.add_field(name="Link tải", value=f"[Google Drive]({link_tai})", inline=False)
    
    if anh_bia != "N/A":
        embed.set_thumbnail(url=anh_bia)
    
    return embed

# ==========================================
# PHẦN 4: HỆ THỐNG AUTO & SYNC (QUAN TRỌNG)
# ==========================================

async def sync_initial_data():
    """
    Chạy khi lần đầu bot khởi động (chưa có cache).
    Lưu lại toàn bộ ngày hiện tại của các phim đang Public.
    Mục đích: Không spam thông báo cho những phim cũ.
    """
    payload_filter = {
        "filter": { "property": "Public", "checkbox": { "equals": True } }
    }
    
    print("⏳ Đang tải toàn bộ dữ liệu (có thể mất vài giây)...")
    all_pages = await fetch_all_pages(payload_filter)
    
    local_cache = {}
    for page in all_pages:
        page_id = page["id"]
        update_date = get_prop(page, "Ngày cập nhật") # Hàm này trả về ngày hoặc None
        if update_date:
            local_cache[page_id] = update_date
            
    save_cache(local_cache)
    print(f"--> Đã lưu trữ {len(local_cache)} phim vào bộ nhớ.")

@tasks.loop(minutes=10)
async def check_new_anime():
    if not CHANNEL_ID: return

    # 1. Lọc ngay từ Notion: Chỉ lấy những phim ĐÃ PUBLIC
    payload_filter = {
        "filter": { "property": "Public", "checkbox": { "equals": True } }
    }
    
    # Dùng hàm fetch_all_pages để lấy HẾT (kể cả > 100 phim)
    all_pages = await fetch_all_pages(payload_filter)
    
    if not all_pages: return

    # 2. Đọc bộ nhớ (Cache) để lấy ngày cũ
    local_cache = load_cache()
    has_changes = False
    channel = client.get_channel(int(CHANNEL_ID))
    
    if not channel:
        print(f"Lỗi: Không tìm thấy kênh ID {CHANNEL_ID}")
        return

    # 3. Duyệt từng phim để so sánh ngày
    for page in all_pages:
        page_id = page["id"]
        new_date = get_prop(page, "Ngày cập nhật")
        
        # Nếu phim không có ngày cập nhật, bỏ qua
        if not new_date: continue

        old_date = local_cache.get(page_id)

        # === ĐIỀU KIỆN TIÊN QUYẾT ===
        # Do đã lọc Public ở bước 1, ở đây ta chỉ cần so sánh ngày.
        # Logic: Nếu (Chưa từng có trong cache) HOẶC (Ngày mới KHÁC Ngày cũ)
        if (page_id not in local_cache) or (new_date != old_date):
            
            print(f"🔔 Update: {get_prop(page, 'Tên Romanji')} -> {new_date}")
            
            ten_phim = get_prop(page, "Tên Romanji")
            slug_url = create_slug_url(ten_phim, page_id)
            web_link = f"{WEB_BASE_URL}/anime/{slug_url}"
            
            embed = await create_anime_embed(page, web_link)
            
            # Đổi tiêu đề cho đẹp
            if page_id not in local_cache:
                embed.set_author(name="🔥 Anime Mới Tinh!", icon_url="https://cdn-icons-png.flaticon.com/512/2965/2965358.png")
            else:
                embed.set_author(name="🔄 Cập Nhật Mới!", icon_url="https://cdn-icons-png.flaticon.com/512/1680/1680899.png")
            
            series_name = get_prop(page, "Loạt phim")
            series_list = await get_series_list(series_name, ten_phim)
            view = AnimeView(series_list)
            
            await channel.send(embed=embed, view=view)
            
            # Cập nhật ngay vào cache trên RAM
            local_cache[page_id] = new_date
            has_changes = True

    # 4. Nếu có thay đổi, lưu xuống file cache.json để nhớ cho lần sau
    if has_changes:
        save_cache(local_cache)
        print("💾 Đã lưu cache mới.")

# ==========================================
# PHẦN 5: GIAO DIỆN & INTERACTION
# ==========================================

class SeriesSelect(Select):
    def __init__(self, series_movies):
        options = [discord.SelectOption(label=m[:100], description="Bấm để xem") for m in series_movies[:25]]
        super().__init__(placeholder="Cùng loạt phim", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        selected_movie = self.values[0]
        
        payload = { "filter": { "property": "Tên Romanji", "title": { "equals": selected_movie } } }
        data = await fetch_notion(payload)
        
        if data and data.get("results"):
            page = data["results"][0]
            
            ten_phim = get_prop(page, "Tên Romanji")
            slug_url = create_slug_url(ten_phim, page["id"])
            web_link = f"{WEB_BASE_URL}/anime/{slug_url}"
            
            embed = await create_anime_embed(page, web_link)
            
            series_name = get_prop(page, "Loạt phim")
            series_list = await get_series_list(series_name, ten_phim)
            
            view = AnimeView(series_list)
            await interaction.edit_original_response(embed=embed, view=view)

class AnimeView(View):
    def __init__(self, series_movies):
        super().__init__(timeout=600)
        if series_movies:
            self.add_item(SeriesSelect(series_movies))

class AnimePaginationView(View):
    def __init__(self, results):
        super().__init__(timeout=600)
        self.results = results
        self.current_page = 0

    async def update_msg(self, interaction):
        page = self.results[self.current_page]
        ten = get_prop(page, "Tên Romanji")
        slug = create_slug_url(ten, page["id"])
        link = f"{WEB_BASE_URL}/anime/{slug}"
        embed = await create_anime_embed(page, link)
        embed.set_footer(text=f"Phim thứ {self.current_page + 1}/{len(self.results)}")
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀️ Trước", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction, button):
        if self.current_page > 0:
            self.current_page -= 1
            await self.update_msg(interaction)

    @discord.ui.button(label="Sau ▶️", style=discord.ButtonStyle.primary)
    async def next_btn(self, interaction, button):
        if self.current_page < len(self.results) - 1:
            self.current_page += 1
            await self.update_msg(interaction)

# ==========================================
# PHẦN 6: CÁC LỆNH DISCORD
# ==========================================

@client.tree.command(name="timphim", description="Tìm kiếm anime")
@app_commands.describe(ten_phim="Tên phim")
async def timphim(interaction: discord.Interaction, ten_phim: str):
    await interaction.response.defer()
    payload = {
        "filter": {
            "and": [
                { "property": "Public", "checkbox": { "equals": True } },
                { "or": [
                    { "property": "Tên Romanji", "title": { "contains": ten_phim } },
                    { "property": "Tên tiếng Anh", "rich_text": { "contains": ten_phim } }
                ]}
            ]
        }
    }
    data = await fetch_notion(payload)
    if not data or not data.get("results"):
        await interaction.followup.send(f"❌ Không tìm thấy phim: **{ten_phim}**")
        return

    page = data["results"][0]
    
    ten_full = get_prop(page, "Tên Romanji")
    slug = create_slug_url(ten_full, page["id"])
    web_link = f"{WEB_BASE_URL}/anime/{slug}"
    
    embed = await create_anime_embed(page, web_link)
    
    series_name = get_prop(page, "Loạt phim")
    series_list = await get_series_list(series_name, ten_full)
    if series_list:
        text_list = "\n".join([f"• {name}" for name in series_list])
        embed.description += f"\n**Cùng loạt phim:**\n{text_list}\n"

    await interaction.followup.send(embed=embed, view=AnimeView(series_list))

@client.tree.command(name="ngaunhien", description="Random 1 bộ anime")
async def ngaunhien(interaction: discord.Interaction):
    await interaction.response.defer()
    # Lấy 100 phim public ngẫu nhiên
    payload = { "page_size": 100, "filter": { "property": "Public", "checkbox": { "equals": True } } }
    data = await fetch_notion(payload)
    
    if data and data.get("results"):
        page = random.choice(data["results"])
        
        ten_full = get_prop(page, "Tên Romanji")
        slug = create_slug_url(ten_full, page["id"])
        web_link = f"{WEB_BASE_URL}/anime/{slug}"
        
        embed = await create_anime_embed(page, web_link)
        embed.title = f"🎲 Random: {embed.title.replace('🎬 ', '')}"
        
        series_name = get_prop(page, "Loạt phim")
        series_list = await get_series_list(series_name, ten_full)
        if series_list:
             embed.description += f"\n**Cùng loạt phim:**\n" + "\n".join([f"• {n}" for n in series_list])

        await interaction.followup.send(embed=embed, view=AnimeView(series_list))
    else:
        await interaction.followup.send("Kho phim trống!")

@client.tree.command(name="mua", description="Xem phim theo mùa (Slide)")
async def mua(interaction: discord.Interaction, ten_mua: str):
    await interaction.response.defer()
    payload = {
        "filter": {
            "and": [
                { "property": "Public", "checkbox": { "equals": True } },
                { "property": "Năm", "rich_text": { "contains": ten_mua } }
            ]
        },
        "sorts": [{ "property": "Tên Romanji", "direction": "ascending" }]
    }
    data = await fetch_notion(payload)
    if data and data.get("results"):
        results = data["results"]
        
        page = results[0]
        ten = get_prop(page, "Tên Romanji")
        slug = create_slug_url(ten, page["id"])
        link = f"{WEB_BASE_URL}/anime/{slug}"
        
        embed = await create_anime_embed(page, link)
        embed.set_footer(text=f"Phim thứ 1/{len(results)}")
        
        await interaction.followup.send(embed=embed, view=AnimePaginationView(results))
    else:
        await interaction.followup.send(f"Không có phim nào mùa: {ten_mua}")

keep_alive()
client.run(TOKEN)
