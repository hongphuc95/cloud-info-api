import json
import os
from dataclasses import dataclass
from typing import Any, List


from api.db.query import insert_product
from api.db.types import Price, Product
from api.tools.hashing import generate_price_hash, generate_product_hash

from azure.identity import ClientSecretCredential
from azure.mgmt.compute import ComputeManagementClient

from flask import current_app

import requests

base_pricing_url = "https://prices.azure.com/api/retail/prices?$filter=serviceName eq " \
    "'Virtual Machines' and armRegionName eq 'westeurope'"


@dataclass
class ProductRaw():
    currencyCode: str
    tierMinimumUnits: str
    retailPrice: str
    unitPrice: str
    armRegionName: str
    location: str
    effectiveStartDate: str
    meterId: str
    meterName: str
    productId: str
    skuId: str
    productName: str
    skuName: str
    serviceName: str
    serviceId: str
    serviceFamily: str
    unitOfMeasure: str
    type: str
    isPrimaryMeterRegion: bool
    armSkuName: str
    effectiveEndDate: str = ''
    reservationTerm: str = ''


@dataclass
class Response():
    BillingCurrency: str
    CustomerEntityId: str
    CustomerEntityType: str
    Items: Any
    NextPageLink: str
    Count: int


def download_file():
    """Download pricing information file from a provider API."""
    current_app.logger.info('Downloading Azure Pricing API...')
    current_link = ''
    page = 1

    while current_link is not None:
        current_app.logger.info(page)
        if current_link == '':
            current_link = base_pricing_url

        response = requests.get(current_link)

        with open(f'data/azureretail-page-{page}.json', 'wb') as handle:
            handle.write(response.content)

        next_page = response.json()['NextPageLink']
        current_link = next_page
        page += 1

        if page % 100 == 0:
            current_app.logger.info(f'Downloaded {page} pages Azure Pricing API')


def load_file():
    """Iterate over downloaded files and process it."""
    current_app.logger.info('Loading Azure VM Size...')
    vm_size_list = process_vm_size('data/size-azure-vms-westus.json')

    current_app.logger.info('Loading Azure pricing...')
    for filename in os.listdir('data'):
        if filename.startswith('azureretail-page-'):
            current_app.logger.info(f'Loading {filename}...')
            try:
                process_file('data/' + filename, vm_size_list)
            except Exception as e:
                current_app.logger.error(f'Skipping {filename} due to {e}')


def scrape_size():
    """Query VMs instance specification."""
    region = 'westus'

    def get_credentials():
        subscription_id = os.environ.get('AZURE_SUBSCRIPTION_ID')
        credentials = ClientSecretCredential(
            client_id=os.environ.get('AZURE_CLIENT_ID'),
            client_secret=os.environ.get('AZURE_SECRET'),
            tenant_id=os.environ.get('AZURE_TENANT'),
        )
        return credentials, subscription_id

    credentials, subscription_id = get_credentials()
    compute_client = ComputeManagementClient(credentials, subscription_id)
    vm_size_list = [vm_size.serialize() for vm_size in
                    compute_client.virtual_machine_sizes.list(location=region)]

    with open(f'data/size-azure-vms-{region}.json', 'w') as handle:
        json.dump(vm_size_list, handle)


def process_vm_size(file_name: str) -> Any:
    """
    Extract VMs instance specification.

    :file_name (str) File name

    Return VMs specification object
    """
    current_app.logger.info(f'Processing VM Size {file_name}...')

    file = open(file_name,)
    data = json.load(file)
    return data


def process_file(file_name: str, vm_size_list: List):
    """
    Extract product's information from a file and dump it into a database.

    :file_name (str) File name
    :vm_size_list (List) Array of VMs specification

    """

    current_app.logger.info(f'Processing {file_name}...')

    file = open(file_name,)
    data = json.load(file)
    response = Response(**data)
    products = map(lambda x: mapped_product(ProductRaw(**x), vm_size_list),
                   response.Items)
    file.close()
    insert_product(products)


def mapped_product(product_raw: ProductRaw, vm_size_list: List) -> Product:
    """
    Generate product properties based on the product raw information.

    :product_raw (ProductRaw) Product Raw JSON information
    :vm_size_list (List) Array of VMs specification

    Return a mapped product
    """
    product = Product(
        productHash='',
        sku=product_raw.skuId,
        vendorName='azure',
        region=product_raw.armRegionName,
        service=product_raw.serviceName,
        productFamily=product_raw.serviceFamily,
        attributes={
            'effectiveStartDate': product_raw.effectiveStartDate,
            'productId': product_raw.productId,
            'productName': product_raw.productName,
            'serviceId': product_raw.serviceId,
            'serviceFamily': product_raw.serviceFamily,
            'skuName': product_raw.skuName,
            'armSkuName': product_raw.armSkuName,
            'meterId': product_raw.meterId,
            'meterName': product_raw.meterName,
        },
        prices=[]
    )

    # Add vcpu and memory info to attributes
    for vm in vm_size_list:
        if product_raw.armSkuName == vm['name']:
            product.attributes['vcpu'] = vm['numberOfCores']
            product.attributes['memory'] = float(vm['memoryInMB']) / 1024

    product.productHash = generate_product_hash(product)
    product.prices = mapped_price(product, product_raw)

    return product


def mapped_price(product: Product, product_raw: ProductRaw) -> List:
    """
    Generate price properties based on the product.

    :product (Product) Product object
    :product_raw (Any) Product Raw JSON information

    Return an array of mapped price object
    """
    prices = []

    instance_type = product_raw.type
    if product_raw.skuName.endswith('Spot'):
        instance_type = 'Spot'
    elif product_raw.skuName.endswith('Low Priority'):
        instance_type = 'Low Priority'

    price = Price(
        priceHash='',
        purchaseOption=instance_type,
        unit=product_raw.unitOfMeasure,
        price=product_raw.unitPrice,
        effectiveDateStart=product_raw.effectiveStartDate,
        startUsageAmount=product_raw.tierMinimumUnits
    )

    if product_raw.reservationTerm:
        price.termLength = product_raw.reservationTerm

    price.priceHash = generate_price_hash(product, price)
    prices.append(price)
    return prices
