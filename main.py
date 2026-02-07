import os
import discord
import aiohttp 
import random
import re
import json
import unicodedata
from datetime import datetime, timedelta, timezone # <--- THÊM MỚI
from discord import app_commands
from discord.ext import tasks
from discord.ui import View, Button, Select

# --- IMPORT FILE KEEP_ALIVE ---
from keep_alive import keep_alive 

# --- CẤU HÌNH (Lấy trực tiếp từ Environment Variables của Render) ---
TOKEN = os.getenv('DISCORD_TOKEN')
NOTION_TOKEN = os.getenv('NOTION_TOKEN')
DATABASE_ID = os.getenv('NOTION_DATABASE_ID')
CHANNEL_ID = os.getenv('CHANNEL_ID')

# Thay link này bằng link app Render của bạn sau khi deploy xong
WEB_BASE_URL = "https://rmbd.onrender.com" 
CACHE_FILE = "cache.json"

intents = discord.Intents.default()
intents.message_content = True 

# ==========================================
# PHẦN 1: QUẢN LÝ CACHE
# ==========================================
def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_cache(cache_data):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache_data, f, ensure_ascii=False, indent=4)

# ==========================================
# PHẦN 2: CLIENT DISCORD
# ==========================================
class MyClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def on_ready(self):
        print(f'Bot đã online: {self.user}')
        await self.tree.sync()
        
        if not os.path.exists(CACHE_FILE):
            print("⚠️ Chạy lần đầu: Đang đồng bộ dữ liệu...")
            await sync_initial_data()
        else:
            print("✅ Đã có dữ liệu cũ. Sẵn sàng hoạt động.")

        if not check_new_anime.is_running():
            check_new_anime.start()
            print('⏰ Đã bật chế độ tự động kiểm tra (10 phút/lần).')

client = MyClient()

# ==========================================
# PHẦN 3: LOGIC NOTION & XỬ LÝ NGÀY
# ==========================================

async def fetch_notion(payload):
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
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    results = []
    has_more = True
    cursor = None
    payload = { "page_size": 100 }
    if filter_payload:
        payload.update(filter_payload)

    async with aiohttp.ClientSession() as session:
        while has_more:
            if cursor:
                payload["start_cursor"] = cursor
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    print(f"Lỗi tải trang: {resp.status}")
                    break
                data = await resp.json()
                if "results" in data:
                    results.extend(data["results"])
                has_more = data.get("has_more", False)
                cursor = data.get("next_cursor")
    return results

def get_prop(page, prop_name):
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
        return prop["date"]["start"] if prop["date"] else None
    return "N/A"

def create_slug_url(title, page_id):
    value = str(title)
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
    value = re.sub(r'[^\w\s-]', '', value.lower())
    slug = re.sub(r'[-\s]+', '-', value).strip('-')
    suffix = page_id[-4:] 
    return f"{slug}-{suffix}"

# --- HÀM MỚI: KIỂM TRA ĐỘ LỆCH THỜI GIAN ---
def is_recently_updated(last_edited_str, user_update_str):
    """
    So sánh Last Edited Time (Notion) với Ngày cập nhật (User).
    Nếu lệch nhau dưới 5 phút -> Trả về True (Nghĩa là bạn vừa cập nhật ngày).
    """
    if not last_edited_str or not user_update_str:
        return False

    try:
        # 1. Xử lý Last Edited (UTC Notion) -> Chuyển sang giờ VN (UTC+7)
        last_edited_utc = datetime.fromisoformat(last_edited_str.replace("Z", "+00:00"))
        last_edited_vn = last_edited_utc.astimezone(timezone(timedelta(hours=7)))

        # 2. Xử lý Ngày cập nhật (User nhập)
        try:
            # Dạng: January 31, 2026 21:09
            user_date = datetime.strptime(user_update_str, "%B %d, %Y %H:%M")
            user_date = user_date.replace(tzinfo=timezone(timedelta(hours=7)))
        except ValueError:
            # Dạng ISO: 2026-01-31...
            if "T" in user_update_str:
                user_date = datetime.fromisoformat(user_update_str)
                if user_date.tzinfo is None:
                    user_date = user_date.replace(tzinfo=timezone(timedelta(hours=7)))
            else:
                return False

        # 3. Tính độ lệch (Giây)
        diff_seconds = abs((last_edited_vn - user_date).total_seconds())

        # Lệch dưới 300 giây (5 phút) -> OK
        return diff_seconds < 300 

    except Exception as e:
        print(f"⚠️ Lỗi so sánh ngày: {e}")
        return False

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
    if ten_tieng_anh != "Không có": desc += f"**Tên khác:** {ten_tieng_anh}\n"
    if tom_tat != "Không có":
        short = (tom_tat[:250] + '...') if len(tom_tat) > 250 else tom_tat
        desc += f"\n**Nội dung:**\n_{short}_\n"
    embed.description = desc
    
    embed.add_field(name="Tiến độ", value=f"{so_tap_sub}/{so_tap}", inline=True)
    embed.add_field(name="Năm", value=nam, inline=True)
    embed.add_field(name="Trạng thái", value=trang_thai, inline=True)
    if nhom_dich != "N/A": embed.add_field(name="Nhóm dịch", value=nhom_dich, inline=True)
    if link_tai and link_tai != "N/A": embed.add_field(name="Link tải", value=f"[Google Drive]({link_tai})", inline=False)
    if anh_bia != "N/A": embed.set_thumbnail(url=anh_bia)
    return embed

# ==========================================
# PHẦN 4: AUTO SYNC & CHECK NEW (ĐÃ SỬA)
# ==========================================

async def sync_initial_data():
    payload_filter = { "filter": { "property": "Public", "checkbox": { "equals": True } } }
    all_pages = await fetch_all_pages(payload_filter)
    local_cache = {}
    for page in all_pages:
        page_id = page["id"]
        update_date = get_prop(page, "Ngày cập nhật")
        if update_date:
            local_cache[page_id] = update_date
    save_cache(local_cache)

@tasks.loop(minutes=10)
async def check_new_anime():
    if not CHANNEL_ID: return
    # Lấy danh sách phim Public
    payload_filter = { "filter": { "property": "Public", "checkbox": { "equals": True } } }
    
    all_pages = await fetch_all_pages(payload_filter)
    if not all_pages: return

    local_cache = load_cache()
    has_changes = False
    channel = client.get_channel(int(CHANNEL_ID))
    
    if not channel: return

    for page in all_pages:
        page_id = page["id"]
        
        # Lấy 2 mốc thời gian
        last_edited = page["last_edited_time"]  # Hệ thống tự sinh
        user_update = get_prop(page, "Ngày cập nhật") # Bạn nhập
        
        if not user_update: continue

        # === LOGIC MỚI: SO SÁNH LAST EDITED vs NGÀY CẬP NHẬT ===
        is_fresh_update = is_recently_updated(last_edited, user_update)
        
        # Nếu không trùng khớp (nghĩa là sửa lặt vặt hoặc bật public phim cũ)
        if not is_fresh_update:
            # Vẫn cập nhật cache để dữ liệu luôn mới
            if local_cache.get(page_id) != user_update:
                local_cache[page_id] = user_update
                has_changes = True
            continue 

        # === NẾU TRÙNG KHỚP -> KIỂM TRA CACHE ĐỂ THÔNG BÁO ===
        old_date = local_cache.get(page_id)
        
        if user_update != old_date:
            print(f"🔔 Update hợp lệ: {get_prop(page, 'Tên Romanji')}")
            
            ten_phim = get_prop(page, "Tên Romanji")
            slug_url = create_slug_url(ten_phim, page_id)
            web_link = f"{WEB_BASE_URL}/anime/{slug_url}"
            
            embed = await create_anime_embed(page, web_link)
            
            if page_id not in local_cache:
                embed.set_author(name="🔥 Anime Mới Tinh!", icon_url="https://cdn-icons-png.flaticon.com/512/2965/2965358.png")
            else:
                embed.set_author(name="🆕 Cập Nhật Mới!", icon_url="https://cdn-icons-png.flaticon.com/512/1680/1680899.png")
            
            series_name = get_prop(page, "Loạt phim")
            series_list = await get_series_list(series_name, ten_phim)
            view = AnimeView(series_list)
            
            await channel.send(embed=embed, view=view)
            
            local_cache[page_id] = user_update
            has_changes = True

    if has_changes:
        save_cache(local_cache)

# ==========================================
# PHẦN 5: VIEW & INTERACTION (THÊM VIEW TÌM KIẾM)
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

# --- CLASS MỚI CHO TÌM KIẾM ---
class SearchResultSelect(discord.ui.Select):
    def __init__(self, results):
        self.results_map = {page['id']: page for page in results}
        options = []
        for page in results[:25]:
            p_id = page['id']
            title = get_prop(page, "Tên Romanji")
            label = title[:95] + "..." if len(title) > 95 else title
            nam = get_prop(page, "Năm")
            desc = f"Năm: {nam}" if nam != "N/A" else ""
            options.append(discord.SelectOption(label=label, value=p_id, description=desc))
        super().__init__(placeholder="Tìm thấy nhiều phim! Hãy chọn...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        selected_id = self.values[0]
        page = self.results_map.get(selected_id)
        if not page: return

        ten_full = get_prop(page, "Tên Romanji")
        slug = create_slug_url(ten_full, page["id"])
        web_link = f"{WEB_BASE_URL}/anime/{slug}"
        
        embed = await create_anime_embed(page, web_link)
        series_name = get_prop(page, "Loạt phim")
        series_list = await get_series_list(series_name, ten_full)
        await interaction.edit_original_response(embed=embed, view=AnimeView(series_list))

class SearchView(discord.ui.View):
    def __init__(self, results):
        super().__init__(timeout=120)
        self.add_item(SearchResultSelect(results))

# ==========================================
# PHẦN 6: COMMANDS
# ==========================================

@client.tree.command(name="timphim", description="Tìm kiếm anime (Nhập chính xác)")
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

# --- LỆNH TÌM MỚI (SMART SEARCH) ---
@client.tree.command(name="tim", description="Tìm phim theo từ khóa (Có danh sách chọn)")
async def tim(interaction: discord.Interaction, tu_khoa: str):
    await interaction.response.defer()
    
    payload = {
        "filter": {
            "and": [
                { "property": "Public", "checkbox": { "equals": True } },
                { "or": [
                    { "property": "Tên Romanji", "title": { "contains": tu_khoa } },
                    { "property": "Tên tiếng Anh", "rich_text": { "contains": tu_khoa } }
                ]}
            ]
        },
        "sorts": [{ "property": "Tên Romanji", "direction": "ascending" }]
    }
    
    data = await fetch_notion(payload)
    
    if not data or not data.get("results"):
        await interaction.followup.send(f"❌ Không tìm thấy phim nào chứa từ: **{tu_khoa}**")
        return

    results = data["results"]

    if len(results) == 1:
        page = results[0]
        ten_full = get_prop(page, "Tên Romanji")
        slug = create_slug_url(ten_full, page["id"])
        web_link = f"{WEB_BASE_URL}/anime/{slug}"
        embed = await create_anime_embed(page, web_link)
        series_name = get_prop(page, "Loạt phim")
        series_list = await get_series_list(series_name, ten_full)
        await interaction.followup.send(embed=embed, view=AnimeView(series_list))
        return

    view = SearchView(results)
    count = len(results)
    msg = f"🔎 Tìm thấy **{count}** phim khớp với '**{tu_khoa}**'. Hãy chọn bên dưới:"
    if count > 25: msg += "\n*(Chỉ hiển thị 25 kết quả đầu tiên)*"
    await interaction.followup.send(content=msg, view=view)

@client.tree.command(name="ngaunhien", description="Random 1 bộ anime")
async def ngaunhien(interaction: discord.Interaction):
    await interaction.response.defer()
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

@client.tree.command(name="mua", description="Xem phim theo mùa")
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

# ==========================================
# KHỞI CHẠY
# ==========================================
if __name__ == "__main__":
    keep_alive()
    try:
        client.run(TOKEN)
    except Exception as e:
        print(f"Lỗi khởi chạy: {e}")
