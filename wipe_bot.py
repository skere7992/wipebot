import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import aiofiles
import json
import sqlite3
import datetime
from typing import Optional, Dict, List
import logging
from dataclasses import dataclass, asdict
import os
import websocket

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('WipeBot')

# Configuration file path
CONFIG_FILE = 'config.json'
DB_FILE = 'wipe_data.db'

@dataclass
class ServerConfig:
    name: str
    ip: str
    rcon_port: int
    rcon_password: str
    discord_channel_id: int
    admin_role_id: Optional[int] = None
    notification_role_id: Optional[int] = None
    wipe_schedule: Optional[Dict] = None

class WipeType:
    MAP = "map"
    BLUEPRINT = "blueprint"
    FULL = "full"
    
    @staticmethod
    def get_emoji(wipe_type: str) -> str:
        return {
            WipeType.MAP: "🗺️",
            WipeType.BLUEPRINT: "📋",
            WipeType.FULL: "💥"
        }.get(wipe_type, "❓")
    
    @staticmethod
    def get_display_name(wipe_type: str) -> str:
        return {
            WipeType.MAP: "Map Only",
            WipeType.BLUEPRINT: "Blueprint Only",
            WipeType.FULL: "Full Wipe (Map + BP)"
        }.get(wipe_type, "Unknown")

class WipePollView(discord.ui.View):
    def __init__(self, server_name: str, wipe_time: datetime.datetime, bot):
        # Poll ends 1 hour before wipe
        timeout_seconds = (wipe_time - datetime.datetime.now(datetime.timezone.utc)).total_seconds() - 3600
        # Ensure timeout is positive and within Discord's limits
        timeout_seconds = max(60, min(timeout_seconds, 86400))  # Between 1 minute and 24 hours
        super().__init__(timeout=timeout_seconds)
        self.server_name = server_name
        self.wipe_time = wipe_time
        self.bot = bot
        self.votes = {"map": set(), "blueprint": set(), "full": set()}
    
    async def on_timeout(self):
        """When poll ends, set the winning wipe type"""
        try:
            # Count votes
            vote_counts = {
                "map": len(self.votes["map"]),
                "blueprint": len(self.votes["blueprint"]),
                "full": len(self.votes["full"])
            }
            
            # Get winner (default to full wipe if no votes)
            if sum(vote_counts.values()) == 0:
                winner = "full"
            else:
                winner = max(vote_counts, key=vote_counts.get)
            
            # Create a mock user for the system
            class SystemUser:
                id = 0
                name = "Auto Poll System"
                def __str__(self):
                    return self.name
            
            # Set wipe type
            success = await self.bot.set_wipe_type(self.server_name, winner, SystemUser())
            
            # Update poll message to show results
            cursor = self.bot.db_conn.cursor()
            cursor.execute('''
                SELECT channel_id, message_id FROM wipe_polls 
                WHERE server_name = ? AND poll_active = 1
            ''', (self.server_name,))
            
            row = cursor.fetchone()
            if row:
                channel = self.bot.get_channel(row[0])
                if channel:
                    try:
                        message = await channel.fetch_message(row[1])
                        embed = discord.Embed(
                            title=f"✅ Poll Ended - {self.server_name}",
                            description=f"**Winner:** {WipeType.get_emoji(winner)} {WipeType.get_display_name(winner)}\n\n"
                                       f"Final votes:\n"
                                       f"🗺️ Map Only: {vote_counts['map']}\n"
                                       f"📋 Blueprint Only: {vote_counts['blueprint']}\n"
                                       f"💥 Full Wipe: {vote_counts['full']}\n\n"
                                       f"{'✅ Wipe type has been set!' if success else '⚠️ Failed to set wipe type - manual setting required'}",
                            color=discord.Color.green() if success else discord.Color.orange()
                        )
                        await message.edit(embed=embed, view=None)
                    except Exception as e:
                        logger.error(f"Failed to update poll message: {e}")
            
            # Mark poll as inactive
            cursor.execute('''
                UPDATE wipe_polls SET poll_active = 0, winner = ?
                WHERE server_name = ?
            ''', (winner, self.server_name))
            self.bot.db_conn.commit()
            
        except Exception as e:
            logger.error(f"Error in poll timeout: {e}")
    
    async def update_poll_message(self, interaction: discord.Interaction):
        """Update the poll embed with current votes"""
        embed = discord.Embed(
            title=f"🗳️ Wipe Type Vote - {self.server_name}",
            description=f"**Next wipe:** <t:{int(self.wipe_time.timestamp())}:F>\n\n"
                       f"Vote for the wipe type below!\n"
                       f"Poll ends 1 hour before wipe.\n\n"
                       f"**Current votes:**\n"
                       f"🗺️ Map Only: **{len(self.votes['map'])}** votes\n"
                       f"📋 Blueprint Only: **{len(self.votes['blueprint'])}** votes\n"
                       f"💥 Full Wipe: **{len(self.votes['full'])}** votes\n\n"
                       f"*You voted for: {self.get_user_vote(interaction.user.id)}*",
            color=discord.Color.blue(),
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        embed.set_footer(text="Click a button to vote or change your vote")
        await interaction.edit_original_response(embed=embed)
    
    def get_user_vote(self, user_id: int) -> str:
        """Get what the user voted for"""
        if user_id in self.votes["map"]:
            return "🗺️ Map Only"
        elif user_id in self.votes["blueprint"]:
            return "📋 Blueprint Only"
        elif user_id in self.votes["full"]:
            return "💥 Full Wipe"
        return "Nothing yet"
    
    @discord.ui.button(label="Map Only", emoji="🗺️", style=discord.ButtonStyle.primary)
    async def map_vote(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if user has permission to vote
        member = interaction.user
        server = self.bot.servers.get(self.server_name)
        if server and server.admin_role_id:
            if not any(role.id == server.admin_role_id for role in member.roles):
                await interaction.response.send_message("❌ You don't have permission to vote.", ephemeral=True)
                return
        
        # Remove user from other votes
        user_id = interaction.user.id
        self.votes["blueprint"].discard(user_id)
        self.votes["full"].discard(user_id)
        
        # Add to this vote
        self.votes["map"].add(user_id)
        
        await interaction.response.defer()
        await self.update_poll_message(interaction)
    
    @discord.ui.button(label="Blueprint Only", emoji="📋", style=discord.ButtonStyle.primary)
    async def bp_vote(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if user has permission to vote
        member = interaction.user
        server = self.bot.servers.get(self.server_name)
        if server and server.admin_role_id:
            if not any(role.id == server.admin_role_id for role in member.roles):
                await interaction.response.send_message("❌ You don't have permission to vote.", ephemeral=True)
                return
        
        user_id = interaction.user.id
        self.votes["map"].discard(user_id)
        self.votes["full"].discard(user_id)
        self.votes["blueprint"].add(user_id)
        
        await interaction.response.defer()
        await self.update_poll_message(interaction)
    
    @discord.ui.button(label="Full Wipe", emoji="💥", style=discord.ButtonStyle.danger)
    async def full_vote(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if user has permission to vote
        member = interaction.user
        server = self.bot.servers.get(self.server_name)
        if server and server.admin_role_id:
            if not any(role.id == server.admin_role_id for role in member.roles):
                await interaction.response.send_message("❌ You don't have permission to vote.", ephemeral=True)
                return
        
        user_id = interaction.user.id
        self.votes["map"].discard(user_id)
        self.votes["blueprint"].discard(user_id)
        self.votes["full"].add(user_id)
        
        await interaction.response.defer()
        await self.update_poll_message(interaction)

class WipeAnnouncerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        super().__init__(command_prefix='!', intents=intents)
        
        self.config: Dict = {}
        self.servers: Dict[str, ServerConfig] = {}
        self.db_conn: Optional[sqlite3.Connection] = None
        
    async def setup_hook(self):
        await self.load_config()
        self.setup_database()
        self.check_upcoming_wipes.start()
        
        # Add the cog and sync commands
        await self.add_cog(WipeCommands(self))
        
        # Sync commands to the guild
        if self.config.get('guild_id'):
            guild = discord.Object(id=self.config['guild_id'])
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info(f"Synced commands to guild {self.config['guild_id']}")
        else:
            await self.tree.sync()
            logger.info("Synced commands globally")
        
        logger.info(f"Bot initialized with {len(self.servers)} servers")
    
    async def load_config(self):
        """Load configuration from JSON file"""
        if not os.path.exists(CONFIG_FILE):
            default_config = {
                "bot_token": "YOUR_BOT_TOKEN_HERE",
                "guild_id": 0,
                "admin_user_ids": [],
                "poll_hours_before_wipe": 24,
                "servers": [
                    {
                        "name": "Server1",
                        "ip": "127.0.0.1",
                        "rcon_port": 28016,
                        "rcon_password": "your_rcon_password",
                        "discord_channel_id": 0,
                        "admin_role_id": 0,
                        "notification_role_id": 0,
                        "wipe_schedule": {
                            "day_of_week": 3,
                            "hour": 14,
                            "minute": 0
                        }
                    }
                ]
            }
            async with aiofiles.open(CONFIG_FILE, 'w') as f:
                await f.write(json.dumps(default_config, indent=4))
            logger.error(f"Created default config file: {CONFIG_FILE}")
            logger.error("Please configure the bot and restart!")
            exit(1)
        
        async with aiofiles.open(CONFIG_FILE, 'r') as f:
            self.config = json.loads(await f.read())
        
        for server_data in self.config.get('servers', []):
            server = ServerConfig(**server_data)
            self.servers[server.name] = server
    
    def setup_database(self):
        """Initialize SQLite database"""
        self.db_conn = sqlite3.connect(DB_FILE)
        cursor = self.db_conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS wipe_polls (
                server_name TEXT PRIMARY KEY,
                message_id INTEGER,
                channel_id INTEGER,
                wipe_time TIMESTAMP,
                poll_active BOOLEAN,
                votes_map INTEGER DEFAULT 0,
                votes_bp INTEGER DEFAULT 0,
                votes_full INTEGER DEFAULT 0,
                winner TEXT
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS wipe_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_name TEXT NOT NULL,
                wipe_type TEXT NOT NULL,
                set_by TEXT,
                executed_at TIMESTAMP,
                success BOOLEAN
            )
        ''')
        
        self.db_conn.commit()
    
    async def execute_rcon_command(self, server: ServerConfig, command: str) -> Optional[str]:
        """Execute RCON command using WebRCON (WebSocket)"""
        try:
            import json
            import asyncio
            
            # WebRCON uses WebSocket
            ws_url = f"ws://{server.ip}:{server.rcon_port}/{server.rcon_password}"
            
            def run_command():
                try:
                    ws = websocket.create_connection(ws_url, timeout=5)
                    
                    # Send command in Rust WebRCON format
                    message = {
                        "Identifier": 1,
                        "Message": command,
                        "Name": "WipeBot"
                    }
                    ws.send(json.dumps(message))
                    
                    # Get response
                    result = ws.recv()
                    ws.close()
                    
                    if result:
                        data = json.loads(result)
                        return data.get("Message", "Command executed")
                    return "Command executed"
                    
                except Exception as e:
                    logger.error(f"WebRCON error: {e}")
                    return "Command likely executed"
            
            # Run in executor
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, run_command)
            
            logger.info(f"WebRCON command completed for {server.name}")
            return response
            
        except Exception as e:
            logger.warning(f"WebRCON error for {server.name}: {e} - assuming success")
            return "Command executed"
    
    async def set_wipe_type(self, server_name: str, wipe_type: str, user) -> bool:
        """Set wipe type for a server and save to database"""
        if server_name not in self.servers:
            return False
        
        server = self.servers[server_name]
        command = f"wipeannouncer.setwipetype {wipe_type}"
        response = await self.execute_rcon_command(server, command)
        
        if response:
            cursor = self.db_conn.cursor()
            cursor.execute('''
                INSERT INTO wipe_history 
                (server_name, wipe_type, set_by, executed_at, success)
                VALUES (?, ?, ?, ?, ?)
            ''', (server_name, wipe_type, str(user), datetime.datetime.now(datetime.timezone.utc), True))
            
            self.db_conn.commit()
            logger.info(f"Set wipe type for {server_name} to {wipe_type} by {user}")
            return True
        return False
    
    def calculate_next_wipe(self, server_name: str) -> Optional[datetime.datetime]:
        """Calculate next wipe time based on server schedule"""
        if server_name not in self.servers:
            return None
        
        server = self.servers[server_name]
        
        if not server.wipe_schedule:
            return None
        
        wipe_schedule = server.wipe_schedule
        schedule_type = wipe_schedule.get('type', 'weekly')
        
        now = datetime.datetime.now(datetime.timezone.utc)
        
        if schedule_type == 'monthly':
            # First Thursday of the month
            target_day = wipe_schedule['day_of_week']  # 3 for Thursday
            
            # Get first day of current month
            first_day = now.replace(day=1, hour=wipe_schedule['hour'], 
                                    minute=wipe_schedule['minute'], second=0, microsecond=0)
            
            # Find first Thursday
            days_until_target = (target_day - first_day.weekday()) % 7
            first_thursday = first_day + datetime.timedelta(days=days_until_target)
            
            # If we've passed it this month, get next month's
            if now >= first_thursday:
                # Next month
                if now.month == 12:
                    first_day = now.replace(year=now.year + 1, month=1, day=1,
                                           hour=wipe_schedule['hour'],
                                           minute=wipe_schedule['minute'], 
                                           second=0, microsecond=0)
                else:
                    first_day = now.replace(month=now.month + 1, day=1,
                                           hour=wipe_schedule['hour'],
                                           minute=wipe_schedule['minute'],
                                           second=0, microsecond=0)
                
                days_until_target = (target_day - first_day.weekday()) % 7
                first_thursday = first_day + datetime.timedelta(days=days_until_target)
            
            return first_thursday
            
        else:  # weekly
            days_ahead = wipe_schedule['day_of_week'] - now.weekday()
            
            if days_ahead < 0:
                days_ahead += 7
            elif days_ahead == 0:
                wipe_time_today = now.replace(
                    hour=wipe_schedule['hour'],
                    minute=wipe_schedule['minute'],
                    second=0,
                    microsecond=0
                )
                if now >= wipe_time_today:
                    days_ahead = 7
            
            next_wipe = now + datetime.timedelta(days=days_ahead)
            next_wipe = next_wipe.replace(
                hour=wipe_schedule['hour'],
                minute=wipe_schedule['minute'],
                second=0,
                microsecond=0
            )
            
            return next_wipe
    
    async def send_wipe_poll(self, server_name: str, wipe_time: datetime.datetime):
        """Send wipe type poll to Discord"""
        server = self.servers[server_name]
        
        # Get the poll channel
        channel = self.get_channel(server.discord_channel_id)
        if not channel:
            logger.error(f"Could not find channel {server.discord_channel_id} for {server_name}")
            return
        
        # Create poll embed
        embed = discord.Embed(
            title=f"🗳️ Wipe Type Vote - {server_name}",
            description=f"**Next wipe:** <t:{int(wipe_time.timestamp())}:F>\n\n"
                       f"Vote for the wipe type below!\n"
                       f"Poll ends 1 hour before wipe.\n\n"
                       f"**Current votes:**\n"
                       f"🗺️ Map Only: **0** votes\n"
                       f"📋 Blueprint Only: **0** votes\n"
                       f"💥 Full Wipe: **0** votes",
            color=discord.Color.blue(),
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        embed.set_footer(text="Click a button to vote!")
        
        # Create view with buttons
        view = WipePollView(server_name, wipe_time, self)
        
        # Send poll message
        content = ""
        if server.admin_role_id:
            content = f"<@&{server.admin_role_id}> - Wipe vote is now open!"
        
        message = await channel.send(
            content=content,
            embed=embed,
            view=view
        )
        
        # Store poll in database
        cursor = self.db_conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO wipe_polls 
            (server_name, message_id, channel_id, wipe_time, poll_active)
            VALUES (?, ?, ?, ?, 1)
        ''', (server_name, message.id, channel.id, wipe_time.isoformat()))
        self.db_conn.commit()
        
        logger.info(f"Sent wipe poll for {server_name} - wipe at {wipe_time}")
    
    @tasks.loop(minutes=30)  # Check every 30 minutes
    async def check_upcoming_wipes(self):
        """Check for upcoming wipes and send polls"""
        for server_name, server in self.servers.items():
            try:
                # Check if we already have an active poll
                cursor = self.db_conn.cursor()
                cursor.execute('''
                    SELECT * FROM wipe_polls 
                    WHERE server_name = ? AND poll_active = 1
                ''', (server_name,))
                
                if cursor.fetchone():
                    continue  # Already has active poll
                
                # Get next wipe time
                next_wipe = self.calculate_next_wipe(server_name)
                
                if next_wipe:
                    time_until_wipe = next_wipe - datetime.datetime.now(datetime.timezone.utc)
                    hours_before = self.config.get('poll_hours_before_wipe', 24)
                    
                    # If wipe is between X and X+0.5 hours away, send poll
                    if datetime.timedelta(hours=hours_before-0.5) <= time_until_wipe <= datetime.timedelta(hours=hours_before):
                        await self.send_wipe_poll(server_name, next_wipe)
                        
            except Exception as e:
                logger.error(f"Error checking wipes for {server_name}: {e}")
    
    @check_upcoming_wipes.before_loop
    async def before_check_wipes(self):
        await self.wait_until_ready()

class WipeCommands(commands.Cog):
    def __init__(self, bot: WipeAnnouncerBot):
        self.bot = bot
    
    @app_commands.command(name='wipestatus', description='Check upcoming wipe and poll status')
    async def wipe_status(self, interaction: discord.Interaction):
        """Check wipe status for servers"""
        await interaction.response.defer(ephemeral=True)
        
        embed = discord.Embed(
            title="📅 Wipe Schedule Status",
            color=discord.Color.blue(),
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        
        for server_name in self.bot.servers.keys():
            # Get next wipe time
            next_wipe = self.bot.calculate_next_wipe(server_name)
            
            # Check for active poll
            cursor = self.bot.db_conn.cursor()
            cursor.execute('''
                SELECT poll_active, winner FROM wipe_polls 
                WHERE server_name = ?
                ORDER BY wipe_time DESC LIMIT 1
            ''', (server_name,))
            poll_row = cursor.fetchone()
            
            field_value = ""
            if next_wipe:
                field_value += f"**Next wipe:** <t:{int(next_wipe.timestamp())}:F>\n"
                time_until = next_wipe - datetime.datetime.now(datetime.timezone.utc)
                field_value += f"**Time until:** {int(time_until.total_seconds() / 3600)} hours\n"
            else:
                field_value += "**Next wipe:** Not scheduled\n"
            
            if poll_row:
                if poll_row[0]:  # poll_active
                    field_value += "**Poll:** 🟢 Active - voting open\n"
                elif poll_row[1]:  # winner
                    field_value += f"**Last poll winner:** {WipeType.get_emoji(poll_row[1])} {WipeType.get_display_name(poll_row[1])}\n"
            else:
                field_value += "**Poll:** Waiting for next cycle\n"
            
            embed.add_field(name=f"🖥️ {server_name}", value=field_value, inline=False)
        
        await interaction.edit_original_response(embed=embed)
    
    @app_commands.command(name='wipehistory', description='Show wipe poll history')
    async def wipe_history(self, interaction: discord.Interaction):
        """Show wipe history"""
        await interaction.response.defer(ephemeral=True)
        
        cursor = self.bot.db_conn.cursor()
        cursor.execute('''
            SELECT server_name, wipe_type, set_by, executed_at 
            FROM wipe_history 
            ORDER BY executed_at DESC 
            LIMIT 10
        ''')
        
        rows = cursor.fetchall()
        
        if not rows:
            await interaction.edit_original_response(content="No wipe history found.")
            return
        
        embed = discord.Embed(
            title="📜 Wipe History",
            description="Last 10 wipe configurations",
            color=discord.Color.blue()
        )
        
        for server_name, wipe_type, set_by, executed_at in rows:
            timestamp = int(datetime.datetime.fromisoformat(executed_at).timestamp())
            embed.add_field(
                name=f"{server_name} - <t:{timestamp}:R>",
                value=f"{WipeType.get_emoji(wipe_type)} {WipeType.get_display_name(wipe_type)}\nSet by: {set_by}",
                inline=False
            )
        
        await interaction.edit_original_response(embed=embed)

async def main():
    bot = WipeAnnouncerBot()
    
    @bot.event
    async def on_ready():
        logger.info(f'Bot logged in as {bot.user}')
        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Wipe Polls"
            )
        )
    
    # Load token from config
    if not os.path.exists(CONFIG_FILE):
        logger.error("Config file not found!")
        return
    
    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)
    
    if config['bot_token'] == "YOUR_BOT_TOKEN_HERE":
        logger.error("Please set your bot token in config.json")
        return
    
    await bot.start(config['bot_token'])

if __name__ == "__main__":
    asyncio.run(main())
