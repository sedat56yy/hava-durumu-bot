import os
import json
import logging
import asyncio
import aiohttp
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from dataclasses import dataclass

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters
)
from telegram.constants import ParseMode

# =============================================================================
# API KEY OTOMATİK VAR TOKEN DEĞİŞ
# TG - @nikecheatyeniden


BOT_TOKEN = "BOT_TOKEN_TELEGRAM_TOKEN"
OPENWEATHER_API_KEY = "1b30ab27b1d6803ff7006daed7b15983"
GEOAPIFY_API_KEY = "8b3a88cc056c4298951e3f23a92e1c9c"

# Veritabanı
DB_PATH = "weather_bot.db"

# Loglama
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
SEARCH_CITY = 1

# =============================================================================
# VERİTABANI
# =============================================================================

class DatabaseManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_database()
    
    def get_connection(self):
        return sqlite3.connect(self.db_path)
    
    def init_database(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_locations (
                    user_id INTEGER PRIMARY KEY,
                    city_name TEXT NOT NULL,
                    lat REAL,
                    lon REAL,
                    country TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS search_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    city_name TEXT,
                    searched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            conn.commit()
    
    def save_favorite(self, user_id: int, city: str, lat: float, lon: float, country: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO user_locations (user_id, city_name, lat, lon, country)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, city, lat, lon, country))
            conn.commit()
    
    def get_favorite(self, user_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT city_name, lat, lon, country FROM user_locations WHERE user_id = ?
            ''', (user_id,))
            row = cursor.fetchone()
            if row:
                return {
                    'city': row[0],
                    'lat': row[1],
                    'lon': row[2],
                    'country': row[3]
                }
            return None
    
    def add_search_history(self, user_id: int, city: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO search_history (user_id, city_name) VALUES (?, ?)
            ''', (user_id, city))
            conn.commit()
    
    def get_search_history(self, user_id: int, limit: int = 5):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT DISTINCT city_name FROM search_history 
                WHERE user_id = ? 
                ORDER BY searched_at DESC 
                LIMIT ?
            ''', (user_id, limit))
            return [row[0] for row in cursor.fetchall()]

# =============================================================================
# HAVA DURUMU SERVİSİ - DÜZELTİLMİŞ
# =============================================================================

class WeatherService:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = None
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    async def search_city(self, query: str, limit: int = 5):
        """Şehir ara - OpenWeatherMap GEO API kullanarak"""
        url = "http://api.openweathermap.org/geo/1.0/direct"
        params = {
            'q': query,
            'limit': limit,
            'appid': self.api_key
        }
        
        async with self.session.get(url, params=params) as response:
            if response.status != 200:
                text = await response.text()
                logger.error(f"Geo API Hatası: {response.status} - {text}")
                raise Exception(f"Geo API Hatası: {response.status}")
            
            data = await response.json()
            
            if not data:
                return []
            
            return [
                {
                    'name': item['name'],
                    'country': item.get('country', ''),
                    'state': item.get('state', ''),
                    'lat': item['lat'],
                    'lon': item['lon']
                }
                for item in data
            ]
    
    async def get_current_weather(self, lat: float, lon: float):
        """Mevcut hava durumu"""
        url = "https://api.openweathermap.org/data/2.5/weather"
        params = {
            'lat': lat,
            'lon': lon,
            'appid': self.api_key,
            'lang': 'tr'
        }
        
        async with self.session.get(url, params=params) as response:
            if response.status != 200:
                text = await response.text()
                logger.error(f"Weather API Hatası: {response.status} - {text}")
                raise Exception(f"Weather API Hatası: {response.status}")
            
            data = await response.json()
            
            return {
                'city': data['name'],
                'country': data['sys'].get('country', ''),
                'temp': data['main']['temp'],
                'feels_like': data['main']['feels_like'],
                'humidity': data['main']['humidity'],
                'pressure': data['main']['pressure'],
                'wind_speed': data['wind']['speed'],
                'wind_deg': data['wind'].get('deg', 0),
                'description': data['weather'][0]['description'].title(),
                'icon': data['weather'][0]['icon'],
                'visibility': data.get('visibility', 10000),
                'sunrise': datetime.fromtimestamp(data['sys']['sunrise']),
                'sunset': datetime.fromtimestamp(data['sys']['sunset']),
                'lat': lat,
                'lon': lon
            }
    
    async def get_forecast(self, lat: float, lon: float, days: int = 5):
        """5 günlük tahmin"""
        url = "https://api.openweathermap.org/data/2.5/forecast"
        params = {
            'lat': lat,
            'lon': lon,
            'appid': self.api_key,
            'lang': 'tr'
        }
        
        async with self.session.get(url, params=params) as response:
            if response.status != 200:
                text = await response.text()
                logger.error(f"Forecast API Hatası: {response.status} - {text}")
                raise Exception(f"Forecast API Hatası: {response.status}")
            
            data = await response.json()
            
            # Günlük gruplama
            daily_data = {}
            for item in data['list']:
                date = datetime.fromtimestamp(item['dt']).date()
                if date not in daily_data:
                    daily_data[date] = []
                daily_data[date].append(item)
            
            forecast = []
            for date, items in list(daily_data.items())[:days]:
                temps = [i['main']['temp'] for i in items]
                forecast.append({
                    'date': datetime.combine(date, datetime.min.time()),
                    'temp_min': min(temps),
                    'temp_max': max(temps),
                    'description': items[0]['weather'][0]['description'].title(),
                    'icon': items[0]['weather'][0]['icon'],
                    'humidity': int(sum(i['main']['humidity'] for i in items) / len(items)),
                    'wind_speed': max(i['wind']['speed'] for i in items),
                    'pop': items[0].get('pop', 0) * 100
                })
            
            return forecast
    
    async def get_air_quality(self, lat: float, lon: float):
        """Hava kalitesi"""
        url = "https://api.openweathermap.org/data/2.5/air_pollution"
        params = {
            'lat': lat,
            'lon': lon,
            'appid': self.api_key
        }
        
        async with self.session.get(url, params=params) as response:
            if response.status != 200:
                text = await response.text()
                logger.error(f"Air Quality API Hatası: {response.status} - {text}")
                raise Exception(f"Air Quality API Hatası: {response.status}")
            
            data = await response.json()
            
            components = data['list'][0]['components']
            aqi = data['list'][0]['main']['aqi']
            
            levels = {
                1: ("🟢 Mükemmel", "Hava kalitesi mükemmel!"),
                2: ("🟡 İyi", "Hava kalitesi kabul edilebilir."),
                3: ("🟠 Orta", "Hassas gruplar dikkatli olmalı."),
                4: ("🔴 Kötü", "Sağlık etkileri olabilir."),
                5: ("🟣 Çok Kötü", "Ciddi sağlık riskleri!")
            }
            
            level, desc = levels.get(aqi, ("❓ Bilinmiyor", ""))
            
            return {
                'aqi': aqi,
                'level': level,
                'desc': desc,
                'pm2_5': components.get('pm2_5', 0),
                'pm10': components.get('pm10', 0),
                'no2': components.get('no2', 0),
                'o3': components.get('o3', 0),
                'co': components.get('co', 0)
            }

# =============================================================================
# YARDIMCI FONKSİYONLAR
# =============================================================================

def get_weather_emoji(icon_code):
    """Hava durumu ikonunu emojiye çevir"""
    icon_map = {
        '01d': '☀️', '01n': '🌙',
        '02d': '⛅', '02n': '☁️',
        '03d': '☁️', '03n': '☁️',
        '04d': '☁️', '04n': '☁️',
        '09d': '🌧️', '09n': '🌧️',
        '10d': '🌦️', '10n': '🌧️',
        '11d': '⛈️', '11n': '⛈️',
        '13d': '❄️', '13n': '❄️',
        '50d': '🌫️', '50n': '🌫️'
    }
    return icon_map.get(icon_code, '🌡️')

def get_wind_direction(deg):
    """Rüzgar yönü"""
    directions = ["Kuzey", "Kuzeydoğu", "Doğu", "Güneydoğu", 
                 "Güney", "Güneybatı", "Batı", "Kuzeybatı"]
    index = round(deg / 45) % 8
    return directions[index]

def create_main_keyboard():
    """Ana menü klavyesi"""
    keyboard = [
        [
            InlineKeyboardButton("🔍 Şehir Ara", callback_data='search_city'),
            InlineKeyboardButton("📍 Konumum", callback_data='my_location')
        ],
        [
            InlineKeyboardButton("⭐ Favorilerim", callback_data='favorites'),
            InlineKeyboardButton("📜 Geçmiş", callback_data='history')
        ],
        [
            InlineKeyboardButton("❓ Yardım", callback_data='help')
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_weather_keyboard(lat, lon, city):
    """Hava durumu sonrası klavye"""
    keyboard = [
        [
            InlineKeyboardButton("📅 5 Günlük Tahmin", callback_data=f'forecast_{lat}_{lon}_{city}'),
            InlineKeyboardButton("💨 Hava Kalitesi", callback_data=f'air_{lat}_{lon}_{city}')
        ],
        [
            InlineKeyboardButton("⭐ Favorilere Ekle", callback_data=f'fav_{lat}_{lon}_{city}'),
            InlineKeyboardButton("🔄 Yenile", callback_data=f'refresh_{lat}_{lon}_{city}')
        ],
        [
            InlineKeyboardButton("◀️ Ana Menü", callback_data='main_menu')
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# =============================================================================
# BOT KOMUTLARI - DÜZELTİLMİŞ
# TG - @nikecheatyeniden =============================================================================

db = DatabaseManager(DB_PATH)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Başlangıç komutu"""
    user = update.effective_user
    
    welcome_text = f"""
🌤️ *Merhaba {user.first_name}!*

Ben *Gelişmiş Hava Durumu Botu*'yum! 

🚀 *Komutlar:*
• `/weather [şehir]` - Hava durumu
• `/forecast [şehir]` - 5 günlük tahmin  
• `/air [şehir]` - Hava kalitesi
• `/favorite` - Favori şehrin
• `/help` - Yardım

*Örnek:* `/weather İstanbul`
    """
    
    await update.message.reply_text(
        welcome_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=create_main_keyboard()
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Yardım komutu"""
    text = """
🌤️ *HAVA DURUMU BOTU - YARDIM*

*Komutlar:*
`/weather İstanbul` - İstanbul hava durumu
`/forecast Ankara` - Ankara 5 günlük tahmin
`/air İzmir` - İzmir hava kalitesi
`/favorite` - Kayıtlı favori şehriniz
`/start` - Ana menü

*Butonlar:*
🔍 Şehir Ara - Şehir ismi yazarak arama
📍 Konumum - Konum paylaşarak hava durumu
⭐ Favorilerim - Kaydedilmiş şehir
📜 Geçmiş - Son aramalar

*Özellikler:*
• Gerçek zamanlı hava durumu
• 5 günlük detaylı tahmin
• Hava kalitesi raporu
• Rüzgar ve nem bilgisi
• Gün doğumu/batımı
"""
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hava durumu komutu - DÜZELTİLMİŞ"""
    if not context.args:
        await update.message.reply_text(
            "❌ Lütfen şehir adı girin.\nÖrnek: `/weather İstanbul`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    city = ' '.join(context.args)
    await show_weather(update, context, city)

async def show_weather(update: Update, context: ContextTypes.DEFAULT_TYPE, city: str, lat=None, lon=None, message=None):
    """Hava durumu göster - DÜZELTİLMİŞ"""
    # Mesajı belirle (callback veya command'dan gelmiş olabilir)
    if message is None:
        if update.message:
            message = update.message
        elif update.callback_query:
            message = update.callback_query.message
    
    # Yükleniyor mesajı
    loading_msg = await message.reply_text("🔍 Hava durumu aranıyor...")
    
    try:
        async with WeatherService(OPENWEATHER_API_KEY) as weather_service:
            # Eğer koordinat verilmediyse şehir ara
            if lat is None or lon is None:
                cities = await weather_service.search_city(city)
                if not cities:
                    await loading_msg.edit_text("❌ Şehir bulunamadı! Lütfen doğru yazdığınızdan emin olun.")
                    return
                
                city_data = cities[0]
                lat, lon = city_data['lat'], city_data['lon']
                city_name = city_data['name']
                country = city_data['country']
            else:
                city_name = city
                country = ""
            
            # Verileri çek
            weather = await weather_service.get_current_weather(lat, lon)
            
            # Hava kalitesini dene (hata olursa önemli değil)
            try:
                air_quality = await weather_service.get_air_quality(lat, lon)
            except:
                air_quality = None
            
            # Veritabanına kaydet
            user_id = update.effective_user.id if update.effective_user else update.callback_query.from_user.id
            db.add_search_history(user_id, city_name)
            
            # Mesajı oluştur
            emoji = get_weather_emoji(weather['icon'])
            temp_c = round(weather['temp'] - 273.15, 1)
            feels_c = round(weather['feels_like'] - 273.15, 1)
            wind_dir = get_wind_direction(weather['wind_deg'])
            
            text = f"""
{emoji} *{weather['city']}, {weather['country']}* Hava Durumu

🌡️ *Sıcaklık:* `{temp_c}°C` (Hissedilen: `{feels_c}°C`)
💧 *Nem:* `%{weather['humidity']}`
🌬️ *Rüzgar:* `{weather['wind_speed']} m/s` ({wind_dir})
👁️ *Görüş:* `{weather['visibility'] / 1000:.1f} km`
🔽 *Basınç:* `{weather['pressure']} hPa`

📝 *Durum:* _{weather['description']}_

🌅 *Gün Doğumu:* `{weather['sunrise'].strftime('%H:%M')}`
🌇 *Gün Batımı:* `{weather['sunset'].strftime('%H:%M')}`
"""
            
            if air_quality:
                text += f"""
💨 *Hava Kalitesi:* {air_quality['level']}
   PM2.5: `{air_quality['pm2_5']:.1f}` µg/m³
"""
            
            text += f"\n⏰ *Güncelleme:* `{datetime.now().strftime('%H:%M')}`"
            
            # Klavye oluştur
            keyboard = create_weather_keyboard(lat, lon, city_name)
            
            # Gönder
            await loading_msg.edit_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard
            )
            
    except Exception as e:
        logger.error(f"Hava durumu hatası: {e}")
        await loading_msg.edit_text(f"❌ Hata oluştu: {str(e)}\n\nLütfen tekrar deneyin.")

async def forecast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tahmin komutu"""
    if not context.args:
        await update.message.reply_text("❌ Lütfen şehir adı girin.\nÖrnek: `/forecast İstanbul`")
        return
    
    city = ' '.join(context.args)
    loading_msg = await update.message.reply_text("📅 Tahminler alınıyor...")
    
    try:
        async with WeatherService(OPENWEATHER_API_KEY) as weather_service:
            cities = await weather_service.search_city(city)
            if not cities:
                await loading_msg.edit_text("❌ Şehir bulunamadı!")
                return
            
            lat, lon = cities[0]['lat'], cities[0]['lon']
            forecasts = await weather_service.get_forecast(lat, lon)
            
            text = "📅 *5 Günlük Tahmin*\n\n"
            days_tr = ['Pzt', 'Sal', 'Çar', 'Per', 'Cum', 'Cmt', 'Paz']
            
            for forecast in forecasts:
                day_name = days_tr[forecast['date'].weekday()]
                emoji = get_weather_emoji(forecast['icon'])
                min_temp = round(forecast['temp_min'] - 273.15, 1)
                max_temp = round(forecast['temp_max'] - 273.15, 1)
                rain_prob = f" 🌧️ %{int(forecast['pop'])}" if forecast['pop'] > 20 else ""
                
                text += f"""{emoji} *{day_name}* - `{forecast['date'].strftime('%d.%m')}`
   🌡️ `{min_temp}°C` / `{max_temp}°C`
   💧 `%{forecast['humidity']}` | 💨 `{forecast['wind_speed']} m/s`{rain_prob}
   _{forecast['description']}_\n\n"""
            
            await loading_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
            
    except Exception as e:
        logger.error(f"Tahmin hatası: {e}")
        await loading_msg.edit_text(f"❌ Hata oluştu: {str(e)}")

async def air_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hava kalitesi komutu"""
    if not context.args:
        await update.message.reply_text("❌ Lütfen şehir adı girin.\nÖrnek: `/air İstanbul`")
        return
    
    city = ' '.join(context.args)
    loading_msg = await update.message.reply_text("💨 Hava kalitesi ölçülüyor...")
    
    try:
        async with WeatherService(OPENWEATHER_API_KEY) as weather_service:
            cities = await weather_service.search_city(city)
            if not cities:
                await loading_msg.edit_text("❌ Şehir bulunamadı!")
                return
            
            lat, lon = cities[0]['lat'], cities[0]['lon']
            air = await weather_service.get_air_quality(lat, lon)
            
            text = f"""
💨 *{cities[0]['name']} Hava Kalitesi Raporu*

{air['level']}

{air['desc']}

📊 *Detaylı Ölçümler:*
• PM2.5: `{air['pm2_5']:.1f}` µg/m³
• PM10: `{air['pm10']:.1f}` µg/m³
• NO₂: `{air['no2']:.1f}` µg/m³
• O₃: `{air['o3']:.1f}` µg/m³
• CO: `{air['co']:.1f}` µg/m³
"""
            await loading_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
            
    except Exception as e:
        logger.error(f"Hava kalitesi hatası: {e}")
        await loading_msg.edit_text(f"❌ Hata oluştu: {str(e)}")

async def favorite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Favori şehir göster"""
    user_id = update.effective_user.id
    favorite = db.get_favorite(user_id)
    
    if not favorite:
        await update.message.reply_text(
            "⭐ Henüz favori konumunuz yok.\n"
            "Hava durumu görüntülerken 'Favorilere Ekle' butonuna tıklayın!"
        )
        return
    
    await show_weather(update, context, favorite['city'], favorite['lat'], favorite['lon'])

# =============================================================================
# CALLBACK HANDLER'LAR - DÜZELTİLMİŞ
# =============================================================================

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tüm callback handler'lar burada - DÜZELTİLMİŞ"""
    query = update.callback_query
    await query.answer()  # Her zaman answer ver
    
    data = query.data
    user_id = query.from_user.id
    
    logger.info(f"Callback received: {data} from user {user_id}")
    
    try:
        if data == 'main_menu':
            await query.edit_message_text(
                "🌤️ *Ana Menü*\n\nBir seçenek seçin:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=create_main_keyboard()
            )
        
        elif data == 'search_city':
            await query.edit_message_text(
                "🔍 Lütfen şehir adı girin:\n\n(Aramayı iptal etmek için /cancel yazın)",
                reply_markup=None
            )
            return SEARCH_CITY
        
        elif data == 'my_location':
            await query.edit_message_text(
                "📍 Lütfen konumunuzu paylaşın:\n\nTelegram'da 📎 eki > Konum > Mevcut Konumu Paylaş",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Geri", callback_data='main_menu')
                ]])
            )
        
        elif data == 'favorites':
            favorite = db.get_favorite(user_id)
            if favorite:
                await show_weather(update, context, favorite['city'], favorite['lat'], favorite['lon'], query.message)
            else:
                await query.answer("⭐ Henüz favoriniz yok!", show_alert=True)
        
        elif data == 'history':
            history = db.get_search_history(user_id)
            if history:
                text = "📜 *Son Aramalarınız:*\n\n" + "\n".join([f"• {h}" for h in history])
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Geri", callback_data='main_menu')
                ]])
                await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            else:
                await query.answer("📜 Henüz arama geçmişiniz yok!", show_alert=True)
        
        elif data == 'help':
            text = """
🌤️ *KOMUTLAR:*
• `/weather [şehir]` - Hava durumu
• `/forecast [şehir]` - 5 günlük tahmin  
• `/air [şehir]` - Hava kalitesi
• `/favorite` - Favori şehrin

*Butonlar:*
🔍 Şehir Ara - Yazarak arama
📍 Konumum - Konum paylaşma
⭐ Favorilerim - Kayıtlı şehir
📜 Geçmiş - Son aramalar
"""
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Geri", callback_data='main_menu')
            ]])
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        
        # Forecast butonu
        elif data.startswith('forecast_'):
            parts = data.split('_')
            if len(parts) >= 4:
                lat = float(parts[1])
                lon = float(parts[2])
                city = '_'.join(parts[3:])  # Şehir adı _ içerebilir
                
                loading_msg = await query.message.reply_text("📅 Tahminler alınıyor...")
                
                async with WeatherService(OPENWEATHER_API_KEY) as weather_service:
                    forecasts = await weather_service.get_forecast(lat, lon)
                    
                    text = f"📅 *{city} - 5 Günlük Tahmin*\n\n"
                    days_tr = ['Pzt', 'Sal', 'Çar', 'Per', 'Cum', 'Cmt', 'Paz']
                    
                    for forecast in forecasts:
                        day_name = days_tr[forecast['date'].weekday()]
                        emoji = get_weather_emoji(forecast['icon'])
                        min_temp = round(forecast['temp_min'] - 273.15, 1)
                        max_temp = round(forecast['temp_max'] - 273.15, 1)
                        rain_prob = f" 🌧️ %{int(forecast['pop'])}" if forecast['pop'] > 20 else ""
                        
                        text += f"""{emoji} *{day_name}* - `{forecast['date'].strftime('%d.%m')}`
   🌡️ `{min_temp}°C` / `{max_temp}°C`{rain_prob}
   _{forecast['description']}_\n\n"""
                    
                    keyboard = InlineKeyboardMarkup([[
                        InlineKeyboardButton("◀️ Geri", callback_data='main_menu')
                    ]])
                    
                    await loading_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        
        # Air quality butonu
        elif data.startswith('air_'):
            parts = data.split('_')
            if len(parts) >= 4:
                lat = float(parts[1])
                lon = float(parts[2])
                city = '_'.join(parts[3:])
                
                loading_msg = await query.message.reply_text("💨 Hava kalitesi ölçülüyor...")
                
                async with WeatherService(OPENWEATHER_API_KEY) as weather_service:
                    air = await weather_service.get_air_quality(lat, lon)
                    
                    text = f"""
💨 *{city} Hava Kalitesi Raporu*

{air['level']}

{air['desc']}

📊 *Detaylı Ölçümler:*
• PM2.5: `{air['pm2_5']:.1f}` µg/m³
• PM10: `{air['pm10']:.1f}` µg/m³
• NO₂: `{air['no2']:.1f}` µg/m³
• O₃: `{air['o3']:.1f}` µg/m³
• CO: `{air['co']:.1f}` µg/m³
"""
                    keyboard = InlineKeyboardMarkup([[
                        InlineKeyboardButton("◀️ Geri", callback_data='main_menu')
                    ]])
                    
                    await loading_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        
        # Favorilere ekle
        elif data.startswith('fav_'):
            parts = data.split('_')
            if len(parts) >= 4:
                lat = float(parts[1])
                lon = float(parts[2])
                city = '_'.join(parts[3:])
                
                db.save_favorite(user_id, city, lat, lon, "")
                await query.answer(f"⭐ {city} favorilere eklendi!", show_alert=True)
        
        # Yenile
        elif data.startswith('refresh_'):
            parts = data.split('_')
            if len(parts) >= 4:
                lat = float(parts[1])
                lon = float(parts[2])
                city = '_'.join(parts[3:])
                
                await show_weather(update, context, city, lat, lon, query.message)
    
    except Exception as e:
        logger.error(f"Callback hatası: {e}")
        await query.message.reply_text(f"❌ Bir hata oluştu: {str(e)}")

async def search_city_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Şehir arama text handler"""
    city = update.message.text
    await show_weather(update, context, city)
    return ConversationHandler.END

async def cancel_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Arama iptal"""
    await update.message.reply_text("❌ Arama iptal edildi.", reply_markup=create_main_keyboard())
    return ConversationHandler.END

async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Konum paylaşımı handler"""
    location = update.message.location
    loading_msg = await update.message.reply_text("📍 Konumunuz analiz ediliyor...")
    
    try:
        async with WeatherService(OPENWEATHER_API_KEY) as weather_service:
            weather = await weather_service.get_current_weather(location.latitude, location.longitude)
            
            try:
                air_quality = await weather_service.get_air_quality(location.latitude, location.longitude)
            except:
                air_quality = None
            
            # Veritabanına kaydet
            db.add_search_history(update.effective_user.id, weather['city'])
            
            # Mesajı oluştur
            emoji = get_weather_emoji(weather['icon'])
            temp_c = round(weather['temp'] - 273.15, 1)
            feels_c = round(weather['feels_like'] - 273.15, 1)
            wind_dir = get_wind_direction(weather['wind_deg'])
            
            text = f"""
{emoji} *{weather['city']}, {weather['country']}* Hava Durumu

🌡️ *Sıcaklık:* `{temp_c}°C` (Hissedilen: `{feels_c}°C`)
💧 *Nem:* `%{weather['humidity']}`
🌬️ *Rüzgar:* `{weather['wind_speed']} m/s` ({wind_dir})
👁️ *Görüş:* `{weather['visibility'] / 1000:.1f} km`
🔽 *Basınç:* `{weather['pressure']} hPa`

📝 *Durum:* _{weather['description']}_

🌅 *Gün Doğumu:* `{weather['sunrise'].strftime('%H:%M')}`
🌇 *Gün Batımı:* `{weather['sunset'].strftime('%H:%M')}`
"""
            
            if air_quality:
                text += f"""
💨 *Hava Kalitesi:* {air_quality['level']}
   PM2.5: `{air_quality['pm2_5']:.1f}` µg/m³
"""
            
            text += f"\n⏰ *Güncelleme:* `{datetime.now().strftime('%H:%M')}`"
            
            keyboard = create_weather_keyboard(location.latitude, location.longitude, weather['city'])
            
            await loading_msg.edit_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard
            )
            
    except Exception as e:
        logger.error(f"Konum hatası: {e}")
        await loading_msg.edit_text(f"❌ Konumunuz analiz edilemedi: {str(e)}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Hata yakalama"""
    logger.error(f"Update {update} caused error {context.error}")
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("❌ Bir hata oluştu. Lütfen tekrar deneyin.")

# =============================================================================
# ANA FONKSİYON
# # TG - @nikecheatyeniden =============================================================================

def main():
    """Botu başlat"""
    logger.info("Bot başlatılıyor...")
    
    # Application oluştur
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Komut handler'ları
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("weather", weather_command))
    application.add_handler(CommandHandler("forecast", forecast_command))
    application.add_handler(CommandHandler("air", air_command))
    application.add_handler(CommandHandler("favorite", favorite_command))
    
    # Conversation handler - Şehir arama
    # TG - @nikecheatyeniden
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_callback, pattern='^search_city$')],
        states={
            SEARCH_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_city_text)]
        },
        fallbacks=[CommandHandler("cancel", cancel_search)],
        per_message=False
    )
    application.add_handler(conv_handler)
    
    # Callback handler - Tüm butonlar için
    # TG - @nikecheatyeniden
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Konum handler
    # TG - @nikecheatyeniden
    application.add_handler(MessageHandler(filters.LOCATION, location_handler))
    
    # Hata handler
    application.add_error_handler(error_handler)
    
    # Botu çalıştır
    logger.info("Bot çalışıyor...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
    
    
   
   #  TG - @nikecheatyeniden
 #  TG - @nikecheatyeniden
# TG - @nikecheatyeniden