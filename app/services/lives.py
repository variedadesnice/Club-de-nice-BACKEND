from supabase import Client
from typing import List, Optional, Dict, Any

class LivesService:
    def __init__(self, supabase: Client):
        self.supabase = supabase

    def get_current_live(self) -> Optional[Dict[str, Any]]:
        # Obtiene el live activo más reciente
        response = self.supabase.table('live_sessions').select('*').eq('is_active', True).order('created_at', desc=True).limit(1).execute()
        if response.data:
            return response.data[0]
        return None

    def create_live(self, title: str, youtube_url: str) -> Dict[str, Any]:
        # Desactivar los lives anteriores
        self.supabase.table('live_sessions').update({'is_active': False}).eq('is_active', True).execute()
        
        # Crear nuevo live
        response = self.supabase.table('live_sessions').insert({
            'title': title,
            'youtube_url': youtube_url,
            'is_active': True
        }).execute()
        return response.data[0]

    def end_live(self, live_id: str) -> Dict[str, Any]:
        response = self.supabase.table('live_sessions').update({'is_active': False}).eq('id', live_id).execute()
        if not response.data:
            raise Exception("Live no encontrado")
        return response.data[0]

    def get_chat_messages(self, live_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        # Obtener mensajes junto con información del autor
        # Usamos un join con la tabla users
        response = self.supabase.table('live_chat_messages') \
            .select('id, content, created_at, live_id, user_id, profiles(name, avatar, role)') \
            .eq('live_id', live_id) \
            .order('created_at', desc=True) \
            .limit(limit) \
            .execute()
        
        messages = []
        # Re-estructurar para que coincida con el schema
        for item in response.data:
            user_data = item.get('profiles', {})
            messages.append({
                'id': item['id'],
                'live_id': item['live_id'],
                'user_id': item['user_id'],
                'content': item['content'],
                'created_at': item['created_at'],
                'author_name': user_data.get('name', 'Usuario'),
                'author_avatar': user_data.get('avatar'),
                'author_role': user_data.get('role', 'member')
            })
        
        # Devolver en orden cronológico (los más antiguos primero para el chat)
        messages.reverse()
        return messages

    def send_chat_message(self, live_id: str, user_id: str, content: str) -> Dict[str, Any]:
        # Verificar que el live existe y está activo
        live = self.supabase.table('live_sessions').select('id, is_active').eq('id', live_id).execute()
        if not live.data or not live.data[0].get('is_active'):
            raise Exception("La transmisión en vivo no está activa o no existe")
            
        response = self.supabase.table('live_chat_messages').insert({
            'live_id': live_id,
            'user_id': user_id,
            'content': content
        }).execute()
        
        item = response.data[0]
        
        # Obtener info del usuario para retornar el mensaje completo
        user_response = self.supabase.table('profiles').select('name, avatar, role').eq('id', user_id).execute()
        user_data = user_response.data[0] if user_response.data else {}
        
        return {
            'id': item['id'],
            'live_id': item['live_id'],
            'user_id': item['user_id'],
            'content': item['content'],
            'created_at': item['created_at'],
            'author_name': user_data.get('name', 'Usuario'),
            'author_avatar': user_data.get('avatar'),
            'author_role': user_data.get('role', 'member')
        }
