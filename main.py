import re
import tempfile
import os
from typing import Optional

try:
    import aiohttp
    import lark_oapi
    from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody
    LARK_AVAILABLE = True
except ImportError:
    LARK_AVAILABLE = False

from pkg.platform.sources.lark import LarkAdapter
from pkg.plugin.context import register, handler, BasePlugin, APIHost, EventContext
from pkg.plugin.events import NormalMessageResponded
import pkg.platform.types.message as platform_message


# 全局图片缓存，所有插件实例共享
_global_image_cache = {}
# 全局会话图片信息，所有插件实例共享
_global_session_images = {}

@register(
    name="MdImgTail",
    description="删除回复中的 Markdown 图片并上传到飞书，在最后以 Markdown 格式添加，仅作用于飞书",
    version="3.1.0",
    author="maijunxuan"
)
class MdImgTail(BasePlugin):

    def __init__(self, host: APIHost):
        super().__init__(host)
        # 匹配 Markdown 图片的正则表达式
        self.img_pattern = re.compile(r'!\[.*?\]\((https?://[^\)]+)\)')
        # 使用全局缓存
        self.image_cache = _global_image_cache
        # 使用全局会话图片信息
        self.session_images = _global_session_images

    async def _download_image(self, url: str) -> bytes:
        """下载图片并返回字节数据"""
        if not LARK_AVAILABLE:
            raise Exception("aiohttp 库未安装")

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
            'Sec-Ch-Ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
            'Sec-Fetch-Dest': 'image',
            'Sec-Fetch-Mode': 'no-cors',
            'Sec-Fetch-Site': 'cross-site',
            'Referer': url,  # 使用图片URL作为Referer
        }

        timeout = aiohttp.ClientTimeout(total=30)  # 30秒超时

        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            try:
                async with session.get(url) as response:
                    if response.status == 200:
                        return await response.read()
                    else:
                        raise Exception(f"下载图片失败，状态码: {response.status}, URL: {url}")
            except aiohttp.ClientError as e:
                raise Exception(f"网络请求失败: {str(e)}, URL: {url}")



    async def _upload_image_to_lark(self, image_url: str, adapter: LarkAdapter) -> Optional[str]:
        """上传图片到飞书并返回image_key"""
        if not LARK_AVAILABLE:
            self.ap.logger.error("lark_oapi 或 aiohttp 库未安装，无法上传图片")
            return None

        # 先检查URL缓存，避免重复下载
        if image_url in self.image_cache:
            self.ap.logger.info(f"图片已缓存，直接返回: {image_url}")
            return self.image_cache[image_url]

        try:
            # 下载图片
            image_bytes = await self._download_image(image_url)

            # 创建临时文件
            with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                temp_file.write(image_bytes)
                temp_file.flush()

                try:
                    # 创建上传请求
                    request = (
                        CreateImageRequest.builder()
                        .request_body(
                            CreateImageRequestBody.builder()
                            .image_type('message')
                            .image(open(temp_file.name, 'rb'))
                            .build()
                        )
                        .build()
                    )

                    # 上传图片
                    response = await adapter.api_client.im.v1.image.acreate(request)

                    if not response.success():
                        raise Exception(f'图片上传失败: {response.code}, {response.msg}')

                    image_key = response.data.image_key
                    self.ap.logger.info(f"image_key: {image_key}")

                    # 缓存结果，使用URL作为key
                    self.image_cache[image_url] = image_key

                    return image_key

                finally:
                    # 清理临时文件
                    try:
                        os.unlink(temp_file.name)
                    except:
                        pass

        except Exception as e:
            self.ap.logger.error(f"上传图片失败: {image_url}, 错误: {str(e)}")
            return None

    def _get_session_id(self, ctx: EventContext) -> str:
        """获取会话ID用于标识不同的对话"""
        # 使用会话对象的哈希值作为会话ID
        return str(hash(str(ctx.event.session)))

    def _is_end_event(self, ctx: EventContext) -> bool:
        """检查是否为__end__事件"""
        try:
            # 检查当前处理的消息是否为__end__事件
            current_message = ctx.event.query.resp_messages[-1]
            return hasattr(current_message, 'name') and current_message.name == '__end__'
        except (IndexError, AttributeError):
            return False

    @handler(NormalMessageResponded)
    async def process_images(self, ctx: EventContext):
        """处理 Markdown 图片：删除并上传到飞书"""
        # 只处理飞书平台
        if not isinstance(ctx.event.query.adapter, LarkAdapter):
            return
        reply_mode = ctx.event.query.adapter.config.get('reply_mode', 'normal')
        if not reply_mode == 'stream_message':
            return

        content = ctx.event.response_text
        session_id = self._get_session_id(ctx)
        is_end = self._is_end_event(ctx)

        # 如果是__end__事件，只添加之前上传的图片，不处理新图片
        if is_end:
            self.ap.logger.info("start end handle")
            if session_id in self.session_images and self.session_images[session_id]:
                # 构建图片的Markdown格式
                image_markdowns = []
                for img_info in self.session_images[session_id]:
                    markdown = f"![{img_info['hover_text']}]({img_info['key']})"
                    image_markdowns.append(markdown)

                # 将图片添加到消息最后面
                if image_markdowns:
                    # 删除 Markdown 图片
                    new_content = self.img_pattern.sub('', content)
                    # 清理多余空行
                    new_content = re.sub(r'\n\s*\n', '\n', new_content)
                    new_content = new_content.strip()
                    if new_content.strip():
                        new_content = new_content + '\n\n' + '\n'.join(image_markdowns)
                    else:
                        new_content = '\n'.join(image_markdowns)

                    ctx.add_return('reply', new_content)

                # 清理会话图片存储
                del self.session_images[session_id]
            return

        # 非__end__事件：查找并处理图片
        image_matches = self.img_pattern.findall(content)

        if not image_matches:
            return

        # 删除 Markdown 图片
        new_content = self.img_pattern.sub('', content)
        # 清理多余空行
        new_content = re.sub(r'\n\s*\n', '\n', new_content)
        new_content = new_content.strip()

        # 初始化会话图片存储
        if session_id not in self.session_images:
            self.session_images[session_id] = []

        # 异步上传图片并存储
        for image_url in image_matches:
            # 检查当前session是否已经处理过这个URL
            existing_urls = [img['url'] for img in self.session_images[session_id]]
            if image_url in existing_urls:
                self.ap.logger.info(f"Session中已存在相同图片URL，跳过: {image_url}")
                continue

            image_key = await self._upload_image_to_lark(image_url, ctx.event.query.adapter)
            if image_key:
                # 存储图片信息
                self.session_images[session_id].append({
                    'url': image_url,
                    'key': image_key,
                    'hover_text': '图片'
                })

        # 更新响应文本
        ctx.add_return('reply', new_content)
