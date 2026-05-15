import requests
import time
from bs4 import BeautifulSoup

headers = {'User-Agent': 'Mozilla/5.0'}
for i in range(5):
    res = requests.post('https://html.duckduckgo.com/html/', data={'q': 'buy apartment'}, headers=headers)
    links = BeautifulSoup(res.text, 'html.parser').select('a.result__a')
    print(i, res.status_code, len(links))
    time.sleep(1)
