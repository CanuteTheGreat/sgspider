#!/usr/bin/env python3
import shutil
import requests
import time
import re
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
import os
import configparser

options = Options()
options.headless = True
#options.headless = False
if os.name == 'nt':
    ff_exec_path = "./geckodriver.exe"
elif os.name == 'posix':
    ff_exec_path = "./geckodriver"
driver = webdriver.Firefox(executable_path=ff_exec_path, options=options)

def getcreds():
    configuration = configparser.ConfigParser()
    configuration.read('sgspider.ini')
    return configuration

def login(credentials):
    driver.get("https://suicidegirls.com")
    time.sleep(1)
    driver.find_element_by_id("login").click()
    time.sleep(1)
    user = driver.find_element_by_name("username")
    password = driver.find_element_by_name("password")
    # Clear the input fields
    user.clear()
    password.clear()
    user.send_keys(credentials['main']['username'])
    password.send_keys(credentials['main']['password'])
    time.sleep(1)
    driver.find_element_by_xpath("//button[@class = 'button call-to-action']").click()
    time.sleep(1)

def getgirls():
    #driver.get("https://suicidegirls.com/photos")
    driver.get("https://www.suicidegirls.com/photos/sg/recent/all/")
    time.sleep(1)
    print("Starting to scroll through photos page.. this will take a *REALLY* LONG time!")
    print("Each '.' in the progress output represents a new page that has been loaded!")
    print("Please be cautious of memory usage!\n\n")
    print("Progress [", end='', flush=True)
    done = False
    cctr = 0
    albumctr = 0
    while done == False:
        albumctr = albumctr + 1
        try:
            driver.find_element_by_xpath("//a[@id = 'load-more']").click()
            print('.', end='', flush=True)
            cctr = 0
        except: 
            print("reached end or next page failed!")
            cctr = cctr + 1
            if cctr >= 10:
                done = True
    print("]\n")
    print("Total albums found: " + str(albumctr))

    urls = []
    elems = driver.find_elements_by_xpath("//a[@href]")
    for elem in elems:
        urls.append(elem.get_attribute("href"))

    girls = []
    for girl in urls:
        if "https" in girl and "album" in girl and "data-comment" not in girl and "members" not in girl and "mailto" not in girl and "twitter.com" not in girl:
            if girl not in girls:
                girls.append(girl)
    return girls

def getimgs(girls):
    for girl in girls:
        driver.get(girl)
        urls = []
        elems = driver.find_elements_by_xpath("//a[@href]")
        for elem in elems:
            urls.append(elem.get_attribute("href"))

        name = girl
        name = name.replace('https://www.suicidegirls.com/girls/', '')
        name = re.sub('/album(.*)', '', name)
        album = girl
        album = re.sub(name, '', album)
        album = album.replace('https://www.suicidegirls.com/girls/', '')
        album = re.sub('/album(.*)[0-9]/', '', album)
        album = re.sub('/', '', album)
        for img in urls:
            if "cloudfront" in img:
                dlimgs(name, album, img)
    # If we reach this we have looped through all the albums, so let's clean things up
    cleanup()

def dlimgs(girl, album, url):
    path = os.path.join("./suicidegirls", girl)
    path = os.path.join(path, album)
    os.makedirs(path, exist_ok=True)   
    filename = os.path.join(path, re.sub('(.*)/', "", os.path.join(path, url)))
    print("Looking at: " + str(url))
    if os.path.exists(filename):
        print("File: " + str(filename) + " already downloaded, skipping!")
        return
    else:
        print("File: "  + str(filename) + " not downloaded, downloading now!")
    response = requests.get(url, stream=True)
    with open(filename, 'wb') as out_file:
        shutil.copyfileobj(response.raw, out_file)
    del response

def cleanup():
    driver.quit()
    quit()

def main():
    login(getcreds())
    getimgs(getgirls())

if __name__ == '__main__':
    main()
