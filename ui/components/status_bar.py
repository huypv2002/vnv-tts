from __future__ import annotations

import ttkbootstrap as tb
from datetime import datetime
from typing import Dict, Optional


class StatusBar:
    def __init__(self, parent) -> None:
        self.frame = tb.Frame(parent, bootstyle="secondary")
        
        # Credits info
        self.credits_label = tb.Label(
            self.frame,
            text="Loading account information...",
            foreground="red",
            font=("Arial", 12, "bold")
        )
        self.credits_label.pack(side="right", padx=10, pady=2)
        
        # Store current user info
        self.current_user_info = None
        
    def update_account_info(self, account_info: Dict) -> None:
        """Update account information from Supabase data"""
        try:
            if 'error' in account_info:
                self.credits_label.config(text=f"Error: {account_info['error']}")
                return
            
            self.current_user_info = account_info
            
            # Extract information
            user = account_info.get('user', {})
            subscription = account_info.get('subscription', {})
            credits = account_info.get('credits', {})
            api_keys = account_info.get('api_keys', {})
            
            # Format display text
            username = user.get('username', 'Unknown')
            # Character-based quota from subscription (count_characters may be NULL)
            raw_chars = subscription.get('count_characters')
            total_chars = int(raw_chars) if raw_chars is not None else 0

            # Convert 12,000,000 → 12.000 (hiển thị theo nghìn ký tự, phân cách bằng '.')
            display_units = total_chars // 1000
            formatted_chars = f"{display_units:,}".replace(",", ".")
            sub_type = subscription.get('type', 'free').upper()
            
            # Format expiry date
            expiry_text = "No expiry"
            if subscription.get('end_date'):
                try:
                    end_date = datetime.fromisoformat(subscription['end_date'].replace('Z', '+00:00'))
                    expiry_text = end_date.strftime("%d/%m/%Y %H:%M")
                except:
                    expiry_text = "Invalid date"
            
            # Format API key info
            active_keys = api_keys.get('active_keys', 0)
            total_keys = api_keys.get('total_keys_available', 0)
            
            # Create status text
            status_text = (
                f"Credits: {formatted_chars} | "
                f"User: {username} | "
                f"Plan: {sub_type} | "
                f"Expires: {expiry_text}"
            )
            
            self.credits_label.config(text=status_text)
            
        except Exception as e:
            self.credits_label.config(text=f"Error updating account info: {str(e)}")
    
    def update_credits_only(self, credits: int) -> None:
        """
        Quick update for remaining characters only.
        `credits` parameter here is treated as remaining characters in the subscription.
        """
        if self.current_user_info:
            # Ensure subscription dict exists
            if 'subscription' not in self.current_user_info or self.current_user_info['subscription'] is None:
                self.current_user_info['subscription'] = {}
            self.current_user_info['subscription']['count_characters'] = int(credits)
            self.update_account_info(self.current_user_info)
        else:
            display_units = int(credits) // 1000
            formatted_chars = f"{display_units:,}".replace(",", ".")
            self.credits_label.config(text=f"Credits: {formatted_chars}")
    
    def update_credits(self, credits: int, email: str, expired_date: str) -> None:
        """Legacy method for backward compatibility"""
        text = f"Credits: {credits} TK: {email} / ExpiredDate: {expired_date}"
        self.credits_label.config(text=text)
    
    def show_loading(self) -> None:
        """Show loading state"""
        self.credits_label.config(text="Loading account information...")
    
    def show_error(self, error_message: str) -> None:
        """Show error state"""
        self.credits_label.config(text=f"Error: {error_message}")
    
    def get_account_summary(self) -> Optional[Dict]:
        """Get current account summary for display in other components"""
        if not self.current_user_info:
            return None
        
        try:
            user = self.current_user_info.get('user', {})
            subscription = self.current_user_info.get('subscription', {})
            credits = self.current_user_info.get('credits', {})
            api_keys = self.current_user_info.get('api_keys', {})
            usage = self.current_user_info.get('usage', {})
            
            return {
                'username': user.get('username'),
                'user_id': user.get('id'),
                'subscription_type': subscription.get('type'),
                'subscription_status': subscription.get('status'),
                'days_remaining': subscription.get('days_remaining'),
                # For backward compatibility, keep key name but it now represents characters remaining
                'total_credits': subscription.get('count_characters'),
                'active_keys': api_keys.get('active_keys'),
                'total_keys': api_keys.get('total_keys_available'),
                'videos_today': usage.get('videos_today'),
                'videos_this_month': usage.get('videos_this_month'),
                'last_updated': self.current_user_info.get('last_updated')
            }
            
        except Exception as e:
            return {'error': str(e)}
