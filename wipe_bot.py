import discord
from discord.ext import commands, tasks
import asyncio
import aiofiles
import json
import sqlite3
import datetime
from typing import Optional, Dict, List
from aiorcon import RCON
import logging
from dataclasses import dataclass, asdict
import os

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

class WipeSelectView(discord.ui.View):
    def __init__(self, server_name: str, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.server_name = server_name
        self.selected_type = None
        self.interaction_user = None
    
    @discord.ui.button(label="Map Only", emoji="🗺️", style=discord.ButtonStyle.primary)
    async def map_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected_type = WipeType.MAP
        self.interaction_user = interaction.user
        await interaction.response.defer()
        self.stop()
    
    @discord.ui.button(label="Blueprint Only", emoji="📋", style=discord.ButtonStyle.primary)
    async def blueprint_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected_type = WipeType.BLUEPRINT
        self.interaction_user = interaction.user
        await interaction.response.defer()
        self.stop()
    
    @discord.ui.button(label="Full Wipe", emoji="💥", style=discord.ButtonStyle.danger)
    async def full_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected_type = WipeType.FULL
        self.interaction_user = interaction.user
        await interaction.response.defer()
        self.stop()
    
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.interaction_user = interaction.user
        await interaction.response.defer()
        self.stop()

class ServerSelectView(discord.ui.View):
    def __init__(self, servers: List[str], timeout: float = 60):
        super().__init__(timeout=timeout)
        self.selected_server = None
        self.add_item(ServerSelectDropdown(servers))

class ServerSelectDropdown(discord.ui.Select):
    def __init__(self, servers: List[str]):
        options = [
            discord.SelectOption(label=server, value=server)
            for server in servers[:25]  # Discord limit
        ]
        super().__init__(
            placeholder="Select a server...",
            min_values=1,
            max_values=1,
            options=options
        )
    
    async def callback(self, interaction: discord.Interaction):
        self.view.selected_server = self.values[0]
        await interaction.response.defer()
        self.view.stop()

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
        self.check_wipe_status.start()
        logger.info(f"Bot initialized with {len(self.servers)} servers")
    
    async def load_config(self):
        """Load configuration from JSON file"""
        if not os.path.exists(CONFIG_FILE):
            default_config = {
                "bot_token": "YOUR_BOT_TOKEN_HERE",
                "guild_id": 0,
                "admin_user_ids": [],
                "servers": [
                    {
                        "name": "Server1",
                        "ip": "127.0.0.1",
                        "rcon_port": 28016,
                        "rcon_password": "your_rcon_password",
                        "discord_channel_id": 0,
                        "admin_role_id": 0,
                        "notification_role_id": 0
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
            CREATE TABLE IF NOT EXISTS wipe_settings (
                server_name TEXT PRIMARY KEY,
                wipe_type TEXT NOT NULL,
                set_by_user_id INTEGER,
                set_by_username TEXT,
                set_at TIMESTAMP,
                last_announcement TIMESTAMP,
                announcement_count INTEGER DEFAULT 0
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS wipe_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_name TEXT NOT NULL,
                wipe_type TEXT NOT NULL,
                set_by_user_id INTEGER,
                set_by_username TEXT,
                executed_at TIMESTAMP,
                success BOOLEAN
            )
        ''')
        
        self.db_conn.commit()
    
    async def execute_rcon_command(self, server: ServerConfig, command: str) -> Optional[str]:
        """Execute RCON command on a server"""
        try:
            from aiorcon import RCON
            
            rcon = RCON(server.ip, server.rcon_port, server.rcon_password)
            await rcon.connect()
            response = await rcon(command)
            rcon.close()
            return response
        except Exception as e:
            logger.error(f"RCON error for {server.name}: {e}")
            return None
    
    async def set_wipe_type(self, server_name: str, wipe_type: str, user: discord.User) -> bool:
        """Set wipe type for a server and save to database"""
        if server_name not in self.servers:
            return False
        
        server = self.servers[server_name]
        command = f"wipeannouncer.setwipetype {wipe_type}"
        response = await self.execute_rcon_command(server, command)
        
        if response:
            cursor = self.db_conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO wipe_settings 
                (server_name, wipe_type, set_by_user_id, set_by_username, set_at, announcement_count)
                VALUES (?, ?, ?, ?, ?, COALESCE((SELECT announcement_count FROM wipe_settings WHERE server_name = ?), 0))
            ''', (server_name, wipe_type, user.id, str(user), datetime.datetime.utcnow(), server_name))
            
            cursor.execute('''
                INSERT INTO wipe_history 
                (server_name, wipe_type, set_by_user_id, set_by_username, executed_at, success)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (server_name, wipe_type, user.id, str(user), datetime.datetime.utcnow(), True))
            
            self.db_conn.commit()
            logger.info(f"Set wipe type for {server_name} to {wipe_type} by {user}")
            return True
        return False
    
    async def get_server_status(self, server_name: str) -> Optional[Dict]:
        """Get current status from server"""
        if server_name not in self.servers:
            return None
        
        server = self.servers[server_name]
        response = await self.execute_rcon_command(server, "wipeannouncer.status")
        
        if response:
            # Parse the response
            status = {}
            for line in response.split('\n'):
                if ':' in line:
                    key, value = line.split(':', 1)
                    status[key.strip().replace('-', '').strip()] = value.strip()
            return status
        return None
    
    @tasks.loop(minutes=5)
    async def check_wipe_status(self):
        """Periodically check server status"""
        for server_name, server in self.servers.items():
            status = await self.get_server_status(server_name)
            if status:
                logger.debug(f"Status for {server_name}: {status}")
    
    @check_wipe_status.before_loop
    async def before_check_status(self):
        await self.wait_until_ready()

class WipeCommands(commands.Cog):
    def __init__(self, bot: WipeAnnouncerBot):
        self.bot = bot
    
    def is_admin(self, user: discord.User) -> bool:
        """Check if user is admin"""
        return user.id in self.bot.config.get('admin_user_ids', [])
    
    def has_server_permission(self, user: discord.Member, server_name: str) -> bool:
        """Check if user has permission for a specific server"""
        if self.is_admin(user):
            return True
        
        if server_name in self.bot.servers:
            server = self.bot.servers[server_name]
            if server.admin_role_id:
                return any(role.id == server.admin_role_id for role in user.roles)
        return False
    
    @commands.command(name='setwipe')
    async def set_wipe(self, ctx: commands.Context, server_name: Optional[str] = None):
        """Set wipe type for a server"""
        if not server_name and len(self.bot.servers) == 1:
            server_name = list(self.bot.servers.keys())[0]
        elif not server_name:
            # Show server selection
            view = ServerSelectView(list(self.bot.servers.keys()))
            embed = discord.Embed(
                title="Select Server",
                description="Choose which server to configure:",
                color=discord.Color.blue()
            )
            msg = await ctx.send(embed=embed, view=view)
            
            await view.wait()
            if view.selected_server:
                server_name = view.selected_server
                await msg.delete()
            else:
                await msg.edit(content="Selection cancelled.", embed=None, view=None)
                return
        
        if not self.has_server_permission(ctx.author, server_name):
            await ctx.send("❌ You don't have permission to manage this server.")
            return
        
        # Show wipe type selection
        view = WipeSelectView(server_name)
        embed = discord.Embed(
            title=f"Set Wipe Type - {server_name}",
            description="Select the type of wipe for the next server restart:",
            color=discord.Color.green()
        )
        embed.add_field(
            name="Options",
            value="🗺️ **Map Only** - Reset map, keep blueprints\n"
                  "📋 **Blueprint Only** - Reset blueprints, keep map\n"
                  "💥 **Full Wipe** - Reset both map and blueprints",
            inline=False
        )
        
        msg = await ctx.send(embed=embed, view=view)
        await view.wait()
        
        if view.selected_type:
            success = await self.bot.set_wipe_type(server_name, view.selected_type, ctx.author)
            if success:
                embed = discord.Embed(
                    title="✅ Wipe Type Set",
                    description=f"**Server:** {server_name}\n"
                               f"**Type:** {WipeType.get_emoji(view.selected_type)} {WipeType.get_display_name(view.selected_type)}\n"
                               f"**Set by:** {ctx.author.mention}",
                    color=discord.Color.green(),
                    timestamp=datetime.datetime.utcnow()
                )
                await msg.edit(embed=embed, view=None)
            else:
                await msg.edit(content="❌ Failed to set wipe type. Check RCON connection.", embed=None, view=None)
        else:
            await msg.edit(content="Selection cancelled.", embed=None, view=None)
    
    @commands.command(name='wipestatus')
    async def wipe_status(self, ctx: commands.Context, server_name: Optional[str] = None):
        """Check wipe status for server(s)"""
        embed = discord.Embed(
            title="Server Wipe Status",
            color=discord.Color.blue(),
            timestamp=datetime.datetime.utcnow()
        )
        
        servers_to_check = [server_name] if server_name else list(self.bot.servers.keys())
        
        for srv_name in servers_to_check:
            if srv_name not in self.bot.servers:
                continue
            
            # Get from database
            cursor = self.bot.db_conn.cursor()
            cursor.execute('''
                SELECT wipe_type, set_by_username, set_at 
                FROM wipe_settings 
                WHERE server_name = ?
            ''', (srv_name,))
            row = cursor.fetchone()
            
            # Get live status
            status = await self.bot.get_server_status(srv_name)
            
            field_value = ""
            if row:
                wipe_type, set_by, set_at = row
                field_value += f"**Configured:** {WipeType.get_emoji(wipe_type)} {WipeType.get_display_name(wipe_type)}\n"
                field_value += f"**Set by:** {set_by}\n"
                field_value += f"**Set at:** <t:{int(datetime.datetime.fromisoformat(set_at).timestamp())}:R>\n"
            
            if status and 'Next Wipe Type' in status:
                field_value += f"**Current in-game:** {status.get('Next Wipe Type', 'Unknown')}"
            
            if not field_value:
                field_value = "No wipe type configured"
            
            embed.add_field(name=srv_name, value=field_value, inline=False)
        
        await ctx.send(embed=embed)
    
    @commands.command(name='forcewipe')
    @commands.has_permissions(administrator=True)
    async def force_wipe(self, ctx: commands.Context, server_name: str):
        """Force wipe announcement immediately"""
        if server_name not in self.bot.servers:
            await ctx.send("❌ Server not found.")
            return
        
        server = self.bot.servers[server_name]
        response = await self.bot.execute_rcon_command(server, "wipeannouncer.force")
        
        if response:
            embed = discord.Embed(
                title="✅ Forced Wipe Announcement",
                description=f"**Server:** {server_name}\n**Response:** {response}",
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)
            
            # Update database
            cursor = self.bot.db_conn.cursor()
            cursor.execute('''
                UPDATE wipe_settings 
                SET last_announcement = ?, announcement_count = announcement_count + 1
                WHERE server_name = ?
            ''', (datetime.datetime.utcnow(), server_name))
            self.bot.db_conn.commit()
        else:
            await ctx.send("❌ Failed to force announcement.")
    
    @commands.command(name='wipehistory')
    async def wipe_history(self, ctx: commands.Context, server_name: Optional[str] = None):
        """Show wipe configuration history"""
        cursor = self.bot.db_conn.cursor()
        
        if server_name:
            cursor.execute('''
                SELECT server_name, wipe_type, set_by_username, executed_at 
                FROM wipe_history 
                WHERE server_name = ? 
                ORDER BY executed_at DESC 
                LIMIT 10
            ''', (server_name,))
        else:
            cursor.execute('''
                SELECT server_name, wipe_type, set_by_username, executed_at 
                FROM wipe_history 
                ORDER BY executed_at DESC 
                LIMIT 10
            ''')
        
        rows = cursor.fetchall()
        
        if not rows:
            await ctx.send("No wipe history found.")
            return
        
        embed = discord.Embed(
            title="Wipe Configuration History",
            color=discord.Color.blue()
        )
        
        for server, wipe_type, set_by, executed_at in rows:
            timestamp = int(datetime.datetime.fromisoformat(executed_at).timestamp())
            embed.add_field(
                name=f"{server} - <t:{timestamp}:R>",
                value=f"{WipeType.get_emoji(wipe_type)} {WipeType.get_display_name(wipe_type)}\nSet by: {set_by}",
                inline=False
            )
        
        await ctx.send(embed=embed)
    
    @commands.command(name='addserver')
    @commands.has_permissions(administrator=True)
    async def add_server(self, ctx: commands.Context):
        """Add a new server (Admin only)"""
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel
        
        embed = discord.Embed(
            title="Add New Server",
            description="Please provide the following information:",
            color=discord.Color.blue()
        )
        await ctx.send(embed=embed)
        
        try:
            # Collect server information
            await ctx.send("**Server name:**")
            name_msg = await self.bot.wait_for('message', check=check, timeout=60)
            name = name_msg.content
            
            await ctx.send("**Server IP:**")
            ip_msg = await self.bot.wait_for('message', check=check, timeout=60)
            ip = ip_msg.content
            
            await ctx.send("**RCON Port:**")
            port_msg = await self.bot.wait_for('message', check=check, timeout=60)
            rcon_port = int(port_msg.content)
            
            await ctx.send("**RCON Password:**")
            pass_msg = await self.bot.wait_for('message', check=check, timeout=60)
            rcon_password = pass_msg.content
            await pass_msg.delete()  # Delete password message
            
            await ctx.send("**Discord Channel ID for notifications:**")
            channel_msg = await self.bot.wait_for('message', check=check, timeout=60)
            channel_id = int(channel_msg.content)
            
            # Create new server config
            new_server = {
                "name": name,
                "ip": ip,
                "rcon_port": rcon_port,
                "rcon_password": rcon_password,
                "discord_channel_id": channel_id
            }
            
            # Add to config
            self.bot.config['servers'].append(new_server)
            
            # Save config
            async with aiofiles.open(CONFIG_FILE, 'w') as f:
                await f.write(json.dumps(self.bot.config, indent=4))
            
            # Add to bot
            server_config = ServerConfig(**new_server)
            self.bot.servers[name] = server_config
            
            embed = discord.Embed(
                title="✅ Server Added",
                description=f"**{name}** has been added successfully!",
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)
            
        except asyncio.TimeoutError:
            await ctx.send("❌ Setup timed out.")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")

async def main():
    bot = WipeAnnouncerBot()
    
    @bot.event
    async def on_ready():
        logger.info(f'Bot logged in as {bot.user}')
        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Server Wipes"
            )
        )
    
    await bot.add_cog(WipeCommands(bot))
    
    # Load token from config
    if not os.path.exists(CONFIG_FILE):
        logger.error("Config file not found! Creating default...")
        default_config = {
            "bot_token": "YOUR_BOT_TOKEN_HERE",
            "guild_id": 0,
            "admin_user_ids": [],
            "servers": []
        }
        with open(CONFIG_FILE, 'w') as f:
            json.dump(default_config, f, indent=4)
        logger.error("Please configure the bot in config.json")
        return
    
    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)
    
    if config['bot_token'] == "YOUR_BOT_TOKEN_HERE":
        logger.error("Please set your bot token in config.json")
        return
    
    await bot.start(config['bot_token'])

if __name__ == "__main__":
    asyncio.run(main())
