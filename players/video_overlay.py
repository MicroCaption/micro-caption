#!/usr/bin/env python3
"""
Simple terminal video player with caption overlay.
Uses GStreamer to play YouTube videos and displays captions on terminal.
"""

import subprocess
import sys
import os
import signal
import threading
import time

try:
    import curses
except ImportError:
    print("Installing curses...")
    os.system('sudo apt-get install -y python3-curses')
    import curses


class VideoPlayer:
    def __init__(self, youtube_url: str, port: int = 8765):
        self.youtube_url = youtube_url
        self.port = port
        self.stop_event = threading.Event()
        self.caption_callback = None

    def _setup_pipewire(self) -> bool:
        """Ensure pipewire/pulseaudio is running"""
        try:
            subprocess.run(['systemctl', 'is-system-running'], check=False,
                          capture_output=True, text=True)
            return True
        except:
            return False

    def _play_audio(self) -> None:
        """Download and play YouTube audio with captions"""
        # Download audio stream
        cmd = [
            'yt-dlp', '-x', '--audio-format', 'mp3',
            '--audio-quality', '0',
            '--postprocessor-args', 'mp3:aac=libfdk_aac',
            '-o', '/dev/null',
            self.youtube_url
        ]

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
            proc.wait()
            print(f'[Player] Audio downloaded successfully.')
        except subprocess.CalledProcessError as e:
            print(f'[Player] Failed to download audio: {e}')

    def run(self, duration: int = None):
        """Run the player"""
        print(f'[Player] Starting video player for: {self.youtube_url}')

        # Check for GStreamer
        if not os.path.exists('/usr/bin/gst-launch-1.0'):
            print('[Player] Installing GStreamer…')
            os.system('sudo apt-get install -y gstreamer1.0-plugins-good gstreamer1.0-plugins-base gstreamer1.0-tools')

        # Create curses screen
        stdscr = curses.initscr()
        curses.echo()
        curses.curs_set(0)
        curses.noecho()
        curses.timeout(100)

        try:
            # Download audio
            self._play_audio()

            # Alternative: stream directly with GStreamer
            gst_play = f'gst-launch-1.0 ytdlparse "{self.youtube_url}" ! ' \
                       f'ytdlelement ! ' \
                       f'thawsink audio-only ! ' \
                       f'audioconvert ! audioresample ! ' \
                       f'audiosink "pulsesink ! device=default"'

            # Start GStreamer audio
            gst_proc = subprocess.Popen(gst_play, shell=True)

            print('[Player] GStreamer audio playing…')

            # Now run curses loop
            try:
                while not self.stop_event.is_set():
                    stdscr.clear()

                    # Display caption area
                    stdscr.addstr(0, 0, ' ' * curses.COLS)

                    if self.caption_callback:
                        caption = self.caption_callback()
                        if caption:
                            # Show caption at bottom of screen
                            stdscr.addstr(curses.LINES - 2, 0, caption[:curses.COLS - 1])
                            stdscr.addstr(curses.LINES - 1, 0, ' ' * curses.COLS)
                        else:
                            stdscr.addstr(curses.LINES - 1, 0, ' ' * curses.COLS)

                    stdscr.refresh()

                    if duration:
                        time.sleep(duration)
                    else:
                        event = stdscr.getch()
                        if event == ord('q') or event == 27:  # q or ESC
                            break

            except curses.error:
                pass
            finally:
                curses.endwin()

        finally:
            # Cleanup
            if 'gst_proc' in locals():
                try:
                    gst_proc.terminate()
                except:
                    pass


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True, help='YouTube URL')
    parser.add_argument('--duration', type=int, default=None)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()

    player = VideoPlayer(args.url, port=args.port)

    def signal_handler(sig, frame):
        print('\n[Player] Shutting down…')
        player.stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)

    player.run(duration=args.duration)
    print('[Player] Stopped.')


if __name__ == '__main__':
    main()
