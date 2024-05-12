import boto3
import os
import time
import re
import base64
import boto3
import uuid
import json
import traceback
import copy    
import io

from botocore.config import Config
from PIL import Image
from io import BytesIO
from urllib import parse
from multiprocessing import Process, Pipe
import numpy as np

s3_bucket = os.environ.get('s3_bucket') # bucket name
s3_photo_prefix = os.environ.get('s3_photo_prefix')
path = os.environ.get('path')

list_of_endpoints = [
    "sam-endpoint-2024-04-10-01-35-30",
    "sam-endpoint-2024-04-30-06-08-55"
]

profile_of_Image_LLMs = json.loads(os.environ.get('profile_of_Image_LLMs'))
selected_LLM = 0

seed = 43
cfgScale = 7.5
# height = 1152
# width = 768

smr_client = boto3.client("sagemaker-runtime")
s3_client = boto3.client('s3')   
rekognition_client = boto3.client('rekognition')

secretsmanager = boto3.client('secretsmanager')
def get_secret():
    try:
        get_secret_value_response = secretsmanager.get_secret_value(
            SecretId='bedrock_access_key'
        )
        # print('get_secret_value_response: ', get_secret_value_response)
        secret = json.loads(get_secret_value_response['SecretString'])
        # print('secret: ', secret)
        secret_access_key = json.loads(secret['secret_access_key'])
        access_key_id = json.loads(secret['access_key_id'])
        
        print('length: ', len(access_key_id))
        #for id in access_key_id:
        #    print('id: ', id)
        # print('access_key_id: ', access_key_id)

    except Exception as e:
        raise e
    
    return access_key_id, secret_access_key

access_key_id, secret_access_key = get_secret()
selected_credential = 0
  
def get_client(profile_of_Image_LLMs, selected_LLM, selected_credential):
    profile = profile_of_Image_LLMs[selected_LLM]
    bedrock_region =  profile['bedrock_region']
    modelId = profile['model_id']
    print(f'LLM: {selected_LLM}, bedrock_region: {bedrock_region}, modelId: {modelId}')
    
    print('access_key_id: ', access_key_id[selected_credential])
    # print('selected_credential: ', selected_credential)
                          
    # bedrock   
    boto3_bedrock = boto3.client(
        service_name='bedrock-runtime',
        region_name=bedrock_region,
        aws_access_key_id=access_key_id[selected_credential],
        aws_secret_access_key=secret_access_key[selected_credential],
        config=Config(
            retries = {
                'max_attempts': 30
            }            
        )
    )
        
    return boto3_bedrock, modelId

def img_resize(image):
    imgWidth, imgHeight = image.size 
    
    max_length = 1024

    if imgWidth < imgHeight:
        imgWidth = int(max_length/imgHeight*imgWidth)
        imgWidth = imgWidth-imgWidth%64
        imgHeight = max_length
    else:
        imgHeight = int(max_length/imgWidth*imgHeight)
        imgHeight = imgHeight-imgHeight%64
        imgWidth = max_length 

    image = image.resize((imgWidth, imgHeight), resample=0)
    return image

def load_image(bucket, key): 
    image_obj = s3_client.get_object(Bucket=bucket, Key=key)
    image_content = image_obj['Body'].read()
    img = Image.open(BytesIO(image_content))
    
    width, height = img.size 
    print(f"(original) width: {width}, height: {height}, size: {width*height}")
    
    img = img_resize(img)
    
    return img

def show_labels(img_path, target_label=None):
    if target_label is None:
        Settings = {"GeneralLabels": {"LabelInclusionFilters":[]},"ImageProperties": {"MaxDominantColors":1}}
        print(f"target_label_None : {target_label}")
    else:
        Settings = {"GeneralLabels": {"LabelInclusionFilters":[target_label]},"ImageProperties": {"MaxDominantColors":1}}
        print(f"target_label : {target_label}")
    
    box = None
    
    image = Image.open(img_path).convert('RGB')
    image = img_resize(image)

    buffer = BytesIO()
    image.save(buffer, format='jpeg', quality=100)
    val = buffer.getvalue()
    
    response = rekognition_client.detect_labels(Image={'Bytes': val},
        MaxLabels=15,
        MinConfidence=0.7,
        # Uncomment to use image properties and filtration settings
        Features=["GENERAL_LABELS", "IMAGE_PROPERTIES"],
        Settings=Settings
    )

    imgWidth, imgHeight = image.size       
    color = 'white'

    for item in response['Labels']:
        # print(item)
        if len(item['Instances']) > 0:
            print(item)
            print(item['Name'], item['Confidence'])

            for sub_item in item['Instances']:
                color = sub_item['DominantColors'][0]['CSSColor']
                box = sub_item['BoundingBox']
                break
        break
    try:
        left = imgWidth * box['Left']
        top = imgHeight * box['Top']
        width = imgWidth * box['Width']
        height = imgHeight * box['Height']

        print(f"imgWidth : {imgWidth}, imgHeight : {imgHeight}")
        print('Left: ' + '{0:.0f}'.format(left))
        print('Top: ' + '{0:.0f}'.format(top))
        print('Object Width: ' + "{0:.0f}".format(width))
        print('Object Height: ' + "{0:.0f}".format(height))
        return imgWidth, imgHeight, int(left), int(top), int(width), int(height), color, response
    except:
        print("There is no target label in the image.")
        return _, _, _, _, _, _, _, _, _

def image_to_base64(img) -> str:
    """Converts a PIL Image or local image file path to a base64 string"""
    if isinstance(img, str):
        if os.path.isfile(img):
            print(f"Reading image from file: {img}")
            with open(img, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        else:
            raise FileNotFoundError(f"File {img} does not exist")
    elif isinstance(img, Image.Image):
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("utf-8")
    else:
        raise ValueError(f"Expected str (filename) or PIL Image. Got {type(img)}")

def decode_image(img):
    img = img.encode("utf8") if type(img) == "bytes" else img    
    # print('encoded image: ', img)
    
    buff = BytesIO(base64.b64decode(img))    
    # print('base64 image: ', base64.b64decode(img))
    
    image = Image.open(buff)
    return image

def invoke_endpoint(endpoint_name, payload):
    response = smr_client.invoke_endpoint(
        EndpointName=endpoint_name,
        Accept="application/json",
        ContentType="application/json",
        Body=json.dumps(payload)
    )
    data = response["Body"].read().decode("utf-8")
    return data

def base64_encode_image(image, formats="PNG"):
    buffer = BytesIO()
    image.save(buffer, format=formats)
    img_str = base64.b64encode(buffer.getvalue())
    return img_str

def generate_outpainting_image(boto3_bedrock, modelId, object_img, mask_img, text_prompt):
    body = json.dumps({
        "taskType": "OUTPAINTING",
        "outPaintingParams": {
            "text": text_prompt,              # Optional
            # "negativeText": negative_prompts,    # Optional
            "image": image_to_base64(object_img),      # Required
            # "maskPrompt": mask_prompt,               # One of "maskImage" or "maskPrompt" is required
            "maskImage": image_to_base64(mask_img),  # Input maskImage based on the values 0 (black) or 255 (white) only
        },                                                 
        "imageGenerationConfig": {
            "numberOfImages": 1,
            "quality": "premium",
            # "quality": "standard",
            "cfgScale": cfgScale,
            # "height": height,
            # "width": width,
            "seed": seed
        }
    })
            
    try: 
        response = boto3_bedrock.invoke_model(
            body=body,
            modelId=modelId,
            accept="application/json", 
            contentType="application/json"
        )
        # print('response: ', response)
    except Exception:
        err_msg = traceback.format_exc()
        print('error message: ', err_msg)                    
        
        print('current access_key_id: ', access_key_id[selected_credential])
        print('modelId: ', modelId)
        
        profile = profile_of_Image_LLMs[selected_LLM]
        bedrock_region =  profile['bedrock_region']
        print('bedrock_region: ', bedrock_region)
        
        raise Exception ("Not able to request for bedrock")
                
    # Output processing
    response_body = json.loads(response.get("body").read())
    img_b64 = response_body["images"][0]
    print(f"Output: {img_b64[0:80]}...")
    
    return img_b64

def parallel_process_for_outpainting(conn, object_img, mask_img, text_prompt, object_name, object_key, selected_credential):  
    boto3_bedrock, modelId = get_client(profile_of_Image_LLMs, selected_LLM, selected_credential)
    
    img_b64 =  generate_outpainting_image(boto3_bedrock, modelId, object_img, mask_img, text_prompt)
            
    # upload
    response = s3_client.put_object(
        Bucket=s3_bucket,
        Key=object_key,
        ContentType='image/jpeg',
        Body=base64.b64decode(img_b64)
    )
    # print('response: ', response)
            
    url = path+s3_photo_prefix+'/'+parse.quote(object_name)
    print('url: ', url)
    
    conn.send(url)
    conn.close()

def parallel_process_for_SAM(conn, faceInfo, encode_object_image, imgWidth, imgHeight, endpoint_name):  
    box = faceInfo
    left = imgWidth * box['Left']
    top = imgHeight * box['Top']
    
    print('Left: ' + '{0:.0f}'.format(left))
    print('Top: ' + '{0:.0f}'.format(top))
        
    width = imgWidth * box['Width']
    height = imgHeight * box['Height']
    print('Face Width: ' + "{0:.0f}".format(width))
    print('Face Height: ' + "{0:.0f}".format(height))
    
    inputs = dict(
        encode_image = encode_object_image,
        input_box = [left, top, left+width, top+height]
    )
    predictions = invoke_endpoint(endpoint_name, inputs)
    print('predictions: ', predictions)
        
    mask_image = decode_image(json.loads(predictions)['mask_image'])
    
    conn.send(mask_image)
    conn.close()    
                    
def lambda_handler(event, context):
    global selected_credential, selected_LLM
        
    print(event)
    
    start_time_for_generation = time.time()
    
    jsonBody = json.loads(event['body'])
    print('request body: ', json.dumps(jsonBody))
    
    requestId = jsonBody["requestId"]
    print('requestId: ', requestId)
    bucket = jsonBody["bucket"]   
    key = jsonBody["key"]   
    
    url_original = path+parse.quote(key)
    print('url_original: ', url_original)
    
    if "id" in jsonBody:
        id = jsonBody["id"]
    else:
        # id = uuid.uuid1()
        finename = key.split('/')[-1]
        print('finename: ', finename)
        id = finename.split('.')[0]
    print('id: ', id)
    
    # mask
    ext = key.split('.')[-1]
    if ext == 'jpg':
        ext = 'jpeg'
    
    img = load_image(bucket, key) # load image from bucket    
    object_image = copy.deepcopy(img)
    encode_object_image = base64_encode_image(object_image,formats=ext.upper()).decode("utf-8")

    # detect faces
    buffer = BytesIO()
    img.save(buffer, format='jpeg', quality=100)
    val = buffer.getvalue()

    response = rekognition_client.detect_faces(Image={'Bytes': val},Attributes=['ALL'])
    print('rekognition response: ', response)
    print('number of faces: ', len(response['FaceDetails']))
    
    """
    nfaces = len(response['FaceDetails'])
    if nfaces == 1:
        k = 6
    elif nfaces == 2:
        k = 3
    elif nfaces == 3:
        k = 2
    elif nfaces >= 4:
        k = 2        
    print('# of output images: ', k)
    """
    k = 1

    imgWidth, imgHeight = img.size           
    # outpaint_prompt =['sky','building','forest']   # ['desert', 'sea', 'mount']
    outpaint_prompt =[
            "A futuristic cityscape , focusing purely on the architecture and technology. The scene shows a skyline dominated by towering skyscrapers", 
            "A medieval village with thatched-roof cottages, villagers in period clothing, and a bustling market square during a festival",   
        #    "A panoramic view of a futuristic city by the sea, with a serene waterfront, advanced aquatic transport systems, and shimmering buildings reflecting the setting sun."
            'A festive scene in a future city during a high-tech festival, with streets filled with people in colorful smart fabrics, interactive digital art installations, and joyous music.'
            ] 
    
    index = 1    
    start_time_for_SAM = time.time()
    
    # Earn mask image for faces
    processes = []
    parent_connections = []
    selected_endpoint = 0
    
    print(f"imgWidth : {imgWidth}, imgHeight : {imgHeight}")
    isFirst = False
    for faceDetail in response['FaceDetails']:
        print('The detected face is between ' + str(faceDetail['AgeRange']['Low']) 
              + ' and ' + str(faceDetail['AgeRange']['High']) + ' years old')

        parent_conn, child_conn = Pipe()
        parent_connections.append(parent_conn)
        
        print('selected_endpoint: ', selected_endpoint)
        endpoint_name = list_of_endpoints[selected_endpoint] 
        print('endpoint_name: ', endpoint_name)
        
        process = Process(target=parallel_process_for_SAM, args=(child_conn, faceDetail['BoundingBox'], encode_object_image, imgWidth, imgHeight, endpoint_name))
        processes.append(process)
        
        selected_endpoint = selected_endpoint + 1
        if selected_endpoint >= len(list_of_endpoints):
            selected_endpoint = 0
            
    for process in processes:
        process.start()
                    
    for parent_conn in parent_connections:
        mask_image = parent_conn.recv()
        
        print('merge current mask')      
        if isFirst==False:       
            np_image = np.array(mask_image)
            #print('np_image: ', np_image)
            mask = np.all(np_image == (0, 0, 0), axis=2)
            
            isFirst = True
        else: 
            np_image = np.array(mask_image)            
            mask_new = np.all(np_image == (0, 0, 0), axis=2)
            
            mask = np.logical_or(mask, mask_new)
    
    print('mask: ', mask)
    
    for i, row in enumerate(mask):
        for j, value in enumerate(row):
            if value == True:
                np_image[i, j] = (0, 0, 0)
            else:
                np_image[i, j] = (255, 255, 255)
            
            
            #comp2 = np.where(comp == False, 0, 1)
            #print('comp2: ', comp2)            
            #np.where(np_mask == (255,255,255), '+', '-')
            
            #for i, row in enumerate(np_mask):
            #    for j, c in enumerate(row):
            #        if np.array_equal(c, np.array([0, 0, 0])):
            #            # print(f'({i}, {j}): {c}, {np_image[i, j]}')
            #            np_image[i, j] = np.array([0, 0, 0])

    for process in processes:
        process.join()
        
        """        
        box = faceDetail['BoundingBox']
        left = imgWidth * box['Left']
        top = imgHeight * box['Top']
        print(f"imgWidth : {imgWidth}, imgHeight : {imgHeight}")
        print('Left: ' + '{0:.0f}'.format(left))
        print('Top: ' + '{0:.0f}'.format(top))
        
        width = imgWidth * box['Width']
        height = imgHeight * box['Height']
        print('Face Width: ' + "{0:.0f}".format(width))
        print('Face Height: ' + "{0:.0f}".format(height))
    
        inputs = dict(
            encode_image = encode_object_image,
            input_box = [left, top, left+width, top+height]
        )
        predictions = invoke_endpoint(endpoint_name, inputs)
        print('predictions: ', predictions)
        
        mask_image = decode_image(json.loads(predictions)['mask_image'])
        
        if i==0:
            np_image = np.array(mask_image)
            # print('np_image: ', np_image)
        else:
            np_mask = np.array(mask_image)
            
            # show a color from np_image using for statement
            for i, row in enumerate(np_mask):
                for j, c in enumerate(row):
                    if np.array_equal(c, np.array([0, 0, 0])):
                        # print(f'({i}, {j}): {c}, {np_image[i, j]}')
                        np_image[i, j] = np.array([0, 0, 0])
        """
    
              
    # print('np_image: ', np_image)                         
    merged_mask_image = Image.fromarray(np_image)
            
    # upload mask image for debugging
    pixels = BytesIO()
    merged_mask_image.save(pixels, "png")
    
    fname = 'mask_'+key.split('/')[-1].split('.')[0]    
    pixels.seek(0, 0)
    response = s3_client.put_object(
        Bucket=s3_bucket,
        Key='photo/'+fname+'.png',
        ContentType='image/png',
        Body=pixels
    )
    #print('response: ', response)
        
    end_time_for_SAM = time.time()
    time_for_SAM = end_time_for_SAM - start_time_for_SAM
    print('time_for_SAM: ', time_for_SAM)
    
    object_img = img_resize(object_image)
    mask_img = img_resize(merged_mask_image)
        
    print('start outpainting')      
    generated_urls = []    
    processes = []       
    parent_connections = []         
    for i in range(k):
        parent_conn, child_conn = Pipe()
        parent_connections.append(parent_conn)
                            
        # text_prompt =  f'a human with a {outpaint_prompt[i]} background'
        text_prompt = f'a neatly and well-dressed human with yellow cute robot dog in {outpaint_prompt[i]}'
                
        object_name = f'photo_{id}_{index}.{ext}'
        object_key = f'{s3_photo_prefix}/{object_name}'  # MP3 파일 경로
        print('generated object_key: ', object_key)
            
        process = Process(target=parallel_process_for_outpainting, args=(child_conn, object_img, mask_img, text_prompt, object_name, object_key, selected_credential))
        processes.append(process)
                
        selected_LLM = selected_LLM + 1
        if selected_LLM == len(profile_of_Image_LLMs):
            selected_LLM = 0
        index = index + 1
                            
    for process in processes:
        process.start()
                    
    for parent_conn in parent_connections:
        url = parent_conn.recv()
        generated_urls.append(url)

    for process in processes:
        process.join()
        
    end_time_for_generation = time.time()
    time_for_outpainting = end_time_for_generation - end_time_for_SAM
    print('time_for_outpainting: ', time_for_outpainting)
                    
    
    time_for_photo_generation = end_time_for_generation - start_time_for_generation
    print('time_for_photo_generation: ', time_for_photo_generation)
            
    print('generated_urls: ', json.dumps(generated_urls))
    
    print('len(access_key): ', len(access_key_id))
    print('current access_key_id: ', access_key_id[selected_credential])
    #print('selected_credential: ', selected_credential)
    
    if selected_credential >= len(access_key_id)-1:
        selected_credential = 0
    else:
        selected_credential = selected_credential + 1
        
    result = {            
        "url_original": url_original,
        "url_generated": json.dumps(generated_urls),
        "time_taken": str(time_for_photo_generation)
    }
    print('result: ', result)
        
    return {
        "isBase64Encoded": False,
        'statusCode': 200,
        'body': json.dumps(result)
    }