import requests

x = requests.get('https://wttr.in/geneva?format=j1')
print(x.content)